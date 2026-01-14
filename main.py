import requests
import pandas as pd
from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import pytz
import time
import threading
import sqlite3
import json
import traceback
import os

app = Flask(__name__)

# --- KONFIGURACJA ---
API_URL = "https://v3.football.api-sports.io/fixtures"
# ▼▼▼ TU WKLEJ SWÓJ KLUCZ ▼▼▼
API_KEY = "d1799adc4fcd859df9ef8e2ba96b28f5" 
# ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲

HEADERS = {'x-rapidapi-host': "v3.football.api-sports.io", 'x-rapidapi-key': API_KEY}
PL_TIMEZONE = pytz.timezone('Europe/Warsaw')
DB_FILE = "pewniaczek.db"
progress_status = {"total": 0, "current": 0, "is_running": False, "message": "Start systemu..."}

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30) 
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS matches (id INTEGER PRIMARY KEY, date_str TEXT, timestamp INTEGER, data_json TEXT)')
    conn.commit()
    conn.close()

def process_goal_minutes(events, team_id):
    if not events: return False
    for event in events:
        if event.get('type') == 'Goal' and event.get('team', {}).get('id') == team_id:
            m = event.get('time', {}).get('elapsed', 0)
            if (40 <= m <= 45) or (85 <= m <= 90): return True
    return False

def calculate_smart_stats(team_id, target_type):
    """
    WERSJA OSZCZĘDNA (BATCHING):
    Pobiera historię, a detale (minuty) pobiera w PACZKACH po kilka sztuk naraz.
    """
    try:
        time.sleep(0.05) 
        resp = requests.get(API_URL, headers=HEADERS, params={"team": team_id, "last": 40})
        if resp.status_code != 200: return {'last_10': 0, 'last_6': 0, 'last_3': 0}
        all_matches = resp.json().get('response', [])
        
        filtered = []
        for m in all_matches:
            l_info = m.get('league', {})
            m_type = l_info.get('type', 'League')
            is_f = 'Friendly' in l_info.get('name', '')
            if target_type == 'Friendly' and is_f: filtered.append(m)
            elif target_type == 'Cup' and m_type == 'Cup' and not is_f: filtered.append(m)
            elif target_type == 'League' and m_type == 'League' and not is_f: filtered.append(m)
            if len(filtered) >= 10: break
            
        if not filtered: return {'last_10': 0, 'last_6': 0, 'last_3': 0}

        # BATCHING: Pobierz detale paczką (1 zapytanie zamiast 10)
        ids = "-".join([str(m['fixture']['id']) for m in filtered])
        det_resp = requests.get(API_URL, headers=HEADERS, params={"ids": ids})
        details_map = {d['fixture']['id']: d for d in det_resp.json().get('response', [])} if det_resp.status_code == 200 else {}

        stats = {'last_10': 0, 'last_6': 0, 'last_3': 0}
        for i, m in enumerate(filtered):
            d = details_map.get(m['fixture']['id'])
            if d and d.get('events'):
                opp_id = d['teams']['away']['id'] if d['teams']['home']['id'] == team_id else d['teams']['home']['id']
                if process_goal_minutes(d['events'], opp_id) or process_goal_minutes(d['events'], team_id):
                     stats['last_10'] += 1
                     if i < 6: stats['last_6'] += 1
                     if i < 3: stats['last_3'] += 1
        return stats
    except: return {'last_10': 0, 'last_6': 0, 'last_3': 0}

def worker_task():
    global progress_status
    init_db()
    while True:
        try:
            now_pl = datetime.now(PL_TIMEZONE)
            dates = [now_pl.strftime('%Y-%m-%d'), (now_pl + timedelta(days=1)).strftime('%Y-%m-%d')]
            for date_str in dates:
                progress_status["is_running"] = True
                progress_status["message"] = f"Pobieranie listy: {date_str}"
                
                # Pobranie listy meczów (tanie zapytanie)
                resp = requests.get(API_URL, headers=HEADERS, params={"date": date_str})
                if resp.status_code != 200: continue
                data = resp.json().get('response', [])
                progress_status["total"] = len(data)
                conn = get_db_connection()
                
                for i, item in enumerate(data):
                    try:
                        progress_status["current"] = i + 1
                        f_id = item['fixture']['id']
                        
                        # --- CACHE (OSZCZĘDZANIE LIMITU) ---
                        # Sprawdzamy, czy mamy już statystyki w bazie. Jeśli tak, NIE POBIERAMY ich ponownie.
                        existing = conn.execute("SELECT data_json FROM matches WHERE id=?", (f_id,)).fetchone()
                        saved_stats = None
                        if existing:
                            try:
                                obj = json.loads(existing[0])
                                if obj.get('h_l10') is not None: saved_stats = obj
                            except: pass
                        
                        if saved_stats:
                            # Używamy zapisanych statystyk - KOSZT: 0 ZAPYTAŃ
                            s_h = {'last_10': saved_stats['h_l10'], 'last_6': saved_stats['h_l6'], 'last_3': saved_stats['h_l3']}
                            s_a = {'last_10': saved_stats['a_l10'], 'last_6': saved_stats['a_l6'], 'last_3': saved_stats['a_l3']}
                            v, vc = saved_stats['verdict'], saved_stats['verdict_class']
                        else:
                            # Liczymy od nowa (tylko raz!) - KOSZT: STANDARDOWY
                            l_info = item.get('league', {})
                            m_type = 'Friendly' if 'Friendly' in l_info.get('name','') else ('Cup' if l_info.get('type') == 'Cup' else 'League')
                            s_h = calculate_smart_stats(item['teams']['home']['id'], m_type)
                            s_a = calculate_smart_stats(item['teams']['away']['id'], m_type)
                            v, vc = "-", "text-secondary"
                            if s_h['last_3'] >= 3 or s_a['last_3'] >= 3: v, vc = "TAK!!", "text-success fw-bold"
                            elif s_h['last_6'] >= 3 or s_a['last_6'] >= 3: v, vc = "TAK", "text-success"
                            elif s_h['last_10'] >= 4 or s_a['last_10'] >= 4: v, vc = "MOŻE", "text-warning"

                        # ZAWSZE aktualizujemy wynik i czas (tanie)
                        m_obj = {
                            'id': f_id,
                            'time': datetime.fromisoformat(item['fixture']['date'].replace('Z','+00:00')).astimezone(PL_TIMEZONE).strftime('%H:%M'),
                            'league_country': f"{item['league'].get('name')} ({item['league'].get('country')})",
                            'match_name': f"{item['teams']['home']['name']} - {item['teams']['away']['name']}",
                            'home_name': item['teams']['home']['name'], 'away_name': item['teams']['away']['name'],
                            'stat_h': f"{s_h['last_10']}/10", 'stat_a': f"{s_a['last_10']}/10",
                            'verdict': v, 'verdict_class': vc,
                            'status': item['fixture']['status']['short'],
                            'is_live': item['fixture']['status']['short'] in ['1H','2H','HT','LIVE','ET'],
                            'h_l10': s_h['last_10'], 'h_l6': s_h['last_6'], 'h_l3': s_h['last_3'],
                            'a_l10': s_a['last_10'], 'a_l6': s_a['last_6'], 'a_l3': s_a['last_3'],
                            'formatted_score': f"{item['goals']['home']}-{item['goals']['away']}" if item['goals']['home'] is not None else "vs"
                        }
                        conn.execute('INSERT OR REPLACE INTO matches (id, date_str, timestamp, data_json) VALUES (?,?,?,?)', 
                                     (f_id, date_str, item['fixture']['timestamp'], json.dumps(m_obj)))
                        conn.commit()
                    except: pass
                conn.close()
        except: pass
        progress_status["is_running"] = False
        time.sleep(600) # Śpij 10 minut

@app.route('/progress')
def get_progress(): return jsonify(progress_status)

@app.route('/')
def index():
    now_pl = datetime.now(PL_TIMEZONE)
    date_str = request.args.get('date', now_pl.strftime('%Y-%m-%d'))
    conn = get_db_connection()
    rows = conn.execute('SELECT data_json FROM matches WHERE date_str = ? ORDER BY timestamp ASC', (date_str,)).fetchall()
    conn.close()
    matches = [json.loads(r['data_json']) for r in rows]
    return render_template('index.html', matches=pd.DataFrame(matches), today_date=now_pl.strftime('%Y-%m-%d'), tomorrow_date=(now_pl + timedelta(days=1)).strftime('%Y-%m-%d'), current_view=date_str)

@app.route('/promocje')
def promocje():
    promos = [
        {"name": "Superbet", "title": "Super Przewaga", "desc": "Wygrywasz przy 2 golach przewagi.", "bonus": "3754 PLN", "color": "#ef2f2f", "url": "https://superbet.pl"},
        {"name": "Fortuna", "title": "3x Zakład Bez Ryzyka", "desc": "Zwrot do 100 zł za przegrane.", "bonus": "300 PLN", "color": "#ffc107", "text_color": "dark", "url": "https://efortuna.pl"},
        {"name": "STS", "title": "Wizjoner", "desc": "Typuj za darmo, wygrywaj bonusy.", "bonus": "1660 PLN", "color": "#005ea2", "url": "https://sts.pl"},
        {"name": "Betclic", "title": "Bez Podatku", "desc": "Gra bez podatku na wszystko.", "bonus": "200 PLN", "color": "#d6001e", "url": "https://betclic.pl"},
        {"name": "Fuksiarz", "title": "Cashback", "desc": "Zwrot stawki na konto główne.", "bonus": "500 PLN", "color": "#c60c30", "url": "https://fuksiarz.pl"}
    ]
    return render_template('promocje.html', promos=promos)

@app.route('/lamaki')
def lamaki():
    now_pl = datetime.now(PL_TIMEZONE)
    date_str = request.args.get('date', now_pl.strftime('%Y-%m-%d'))
    conn = get_db_connection()
    rows = conn.execute('SELECT data_json FROM matches WHERE date_str = ? ORDER BY timestamp ASC', (date_str,)).fetchall()
    conn.close()
    matches = [json.loads(r['data_json']) for r in rows]
    return render_template('lamaki.html', matches=pd.DataFrame(matches), today_date=now_pl.strftime('%Y-%m-%d'), tomorrow_date=(now_pl + timedelta(days=1)).strftime('%Y-%m-%d'), current_view=date_str)

def get_detailed_stats_for_analysis(team_id, count=5):
    try:
        resp = requests.get(API_URL, headers=HEADERS, params={"team": team_id, "last": count})
        matches = resp.json().get('response', [])
        formatted = []
        w, d, l, gf, ga = 0, 0, 0, 0, 0
        for m in matches:
            home = m['teams']['home']['id'] == team_id
            f = (m['goals']['home'] or 0) if home else (m['goals']['away'] or 0)
            a = (m['goals']['away'] or 0) if home else (m['goals']['home'] or 0)
            gf += f; ga += a
            res = 'draw'
            if f > a: res = 'home' if home else 'away'; w += 1
            elif f < a: l += 1
            else: d += 1
            formatted.append({
                'home_team': m['teams']['home']['name'], 'away_team': m['teams']['away']['name'],
                'score': f"{m['goals']['home']}-{m['goals']['away']}", 'winner': res
            })
        return {'matches': formatted, 'wins': w, 'draws': d, 'losses': l, 'goals_for': gf, 'goals_against': ga}
    except: return {'matches': [], 'wins': 0, 'draws': 0, 'losses': 0, 'goals_for': 0, 'goals_against': 0}

@app.route('/analyze/<int:fixture_id>')
def analyze(fixture_id):
    try:
        resp = requests.get(API_URL, headers=HEADERS, params={"ids": fixture_id})
        match_data = resp.json().get('response', [])[0]
        h2h_resp = requests.get(API_URL, headers=HEADERS, params={"h2h": f"{match_data['teams']['home']['id']}-{match_data['teams']['away']['id']}"})
        sh = get_detailed_stats_for_analysis(match_data['teams']['home']['id'])
        sa = get_detailed_stats_for_analysis(match_data['teams']['away']['id'])
        return render_template('analysis.html', match=match_data, h2h=h2h_resp.json().get('response', []), 
                             stats_home=sh, stats_away=sa, history_home=sh['matches'], history_away=sa['matches'])
    except Exception as e: return f"Błąd: {e}"

if __name__ == '__main__':
    threading.Thread(target=worker_task, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)