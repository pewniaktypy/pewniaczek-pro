import requests
import pandas as pd
from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import pytz
import time
import threading
import sqlite3
import json
import os

# --- IMPORT KLUCZA Z PLIKU CONFIG.PY ---
try:
    from config import API_KEY
except ImportError:
    API_KEY = os.environ.get("API_KEY", "BRAK_KLUCZA")

app = Flask(__name__)

# --- KONFIGURACJA ---
API_URL = "https://v3.football.api-sports.io/fixtures"
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
    conn.execute('CREATE TABLE IF NOT EXISTS team_stats (team_id INTEGER PRIMARY KEY, lamak_text TEXT, last_updated TEXT)')
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

def get_or_calculate_lamak(team_id, conn):
    try:
        row = conn.execute("SELECT lamak_text FROM team_stats WHERE team_id=?", (team_id,)).fetchone()
        if row: return row[0]
        time.sleep(0.1) 
        resp = requests.get(API_URL, headers=HEADERS, params={"team": team_id, "last": 150})
        matches = resp.json().get('response', [])
        if not matches: result_text = "0 (0%)"
        else:
            total = len(matches)
            lamak_count = 0
            for m in matches:
                s = m.get('score', {})
                ht = s.get('halftime', {})
                ft = s.get('fulltime', {})
                if ht.get('home') is None or ft.get('home') is None: continue
                h_ht, a_ht = ht['home'], ht['away']
                h_ft, a_ft = ft['home'], ft['away']
                if (h_ht > a_ht and h_ft < a_ft) or (a_ht > h_ht and h_ft > a_ft): lamak_count += 1
            percent = round((lamak_count / total) * 100) if total > 0 else 0
            result_text = f"{lamak_count} ({percent}%)"
        conn.execute("INSERT OR REPLACE INTO team_stats (team_id, lamak_text, last_updated) VALUES (?, ?, ?)", 
                     (team_id, result_text, datetime.now().strftime('%Y-%m-%d')))
        conn.commit()
        return result_text
    except: return "?"

def worker_task():
    global progress_status
    init_db()
    while True:
        try:
            now_pl = datetime.now(PL_TIMEZONE)
            dates = [now_pl.strftime('%Y-%m-%d')] 
            for date_str in dates:
                progress_status["is_running"] = True
                progress_status["message"] = f"Pobieranie listy: {date_str}"
                resp = requests.get(API_URL, headers=HEADERS, params={"date": date_str})
                if resp.status_code != 200: continue
                data = resp.json().get('response', [])
                progress_status["total"] = len(data)
                conn = get_db_connection()
                
                for i, item in enumerate(data):
                    try:
                        progress_status["current"] = i + 1
                        f_id = item['fixture']['id']
                        existing = conn.execute("SELECT data_json FROM matches WHERE id=?", (f_id,)).fetchone()
                        saved = None
                        if existing:
                            try:
                                obj = json.loads(existing[0])
                                if obj.get('lamak_h'): saved = obj
                            except: pass
                        
                        if saved:
                            s_h = {'last_10': saved['h_l10'], 'last_6': saved['h_l6'], 'last_3': saved['h_l3']}
                            s_a = {'last_10': saved['a_l10'], 'last_6': saved['a_l6'], 'last_3': saved['a_l3']}
                            l_h, l_a = saved.get('lamak_h', '0 (0%)'), saved.get('lamak_a', '0 (0%)')
                            v, vc = saved['verdict'], saved['verdict_class']
                        else:
                            l_info = item.get('league', {})
                            m_type = 'Friendly' if 'Friendly' in l_info.get('name','') else ('Cup' if l_info.get('type') == 'Cup' else 'League')
                            s_h = calculate_smart_stats(item['teams']['home']['id'], m_type)
                            s_a = calculate_smart_stats(item['teams']['away']['id'], m_type)
                            l_h = get_or_calculate_lamak(item['teams']['home']['id'], conn)
                            l_a = get_or_calculate_lamak(item['teams']['away']['id'], conn)
                            v, vc = "-", "text-secondary"
                            if s_h['last_3'] >= 3 or s_a['last_3'] >= 3: v, vc = "TAK!!", "text-success fw-bold"
                            elif s_h['last_6'] >= 3 or s_a['last_6'] >= 3: v, vc = "TAK", "text-success"
                            elif s_h['last_10'] >= 4 or s_a['last_10'] >= 4: v, vc = "MOŻE", "text-warning"

                        m_obj = {
                            'id': f_id,
                            'time': datetime.fromisoformat(item['fixture']['date'].replace('Z','+00:00')).astimezone(PL_TIMEZONE).strftime('%H:%M'),
                            'league_country': f"{item['league'].get('name')} ({item['league'].get('country')})",
                            'match_name': f"{item['teams']['home']['name']} - {item['teams']['away']['name']}",
                            'home_name': item['teams']['home']['name'], 'away_name': item['teams']['away']['name'],
                            'stat_h': f"{s_h['last_10']}/10", 'stat_a': f"{s_a['last_10']}/10",
                            'lamak_h': l_h, 'lamak_a': l_a,
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
        time.sleep(1800)

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

@app.route('/lamaki')
def lamaki():
    now_pl = datetime.now(PL_TIMEZONE)
    date_str = request.args.get('date', now_pl.strftime('%Y-%m-%d'))
    conn = get_db_connection()
    rows = conn.execute('SELECT data_json FROM matches WHERE date_str = ? ORDER BY timestamp ASC', (date_str,)).fetchall()
    conn.close()
    matches = [json.loads(r['data_json']) for r in rows]
    return render_template('lamaki.html', matches=pd.DataFrame(matches), today_date=now_pl.strftime('%Y-%m-%d'), tomorrow_date=(now_pl + timedelta(days=1)).strftime('%Y-%m-%d'), current_view=date_str)

# --- ZAKTUALIZOWANA TRASA: PROMOCJE (MODERN) ---
@app.route('/promocje')
def promocje():
    promos = [
        {
            "name": "Superbet", "code": "PEWNIAK", "url": "https://superbet.pl/rejestracja?bonus=PEWNIAK", "color": "#ef2f2f",
            "deposit": "X", "freebet": "X", "risk_free": "X", "bonus_val": "3755 PLN"
        },
        {
            "name": "STS", "code": "PEWNIAK", "url": "https://record.stsaff.pl/_zgupHwwvAp7tzisnZM_GtWNd7ZgqdRLk/1/", "color": "#005ea2",
            "deposit": "X", "freebet": "X", "risk_free": "X", "bonus_val": "660 PLN"
        },
        {
            "name": "Fortuna", "code": "PEWNIAK", "url": "https://efortuna.pl", "color": "#ffc107", "text_dark": True,
            "deposit": "", "freebet": "", "risk_free": "3", "bonus_val": "330 PLN"
        },
        {
            "name": "Betclic", "code": "PEWNIAKTYPY", "url": "https://betclic.pl/landing/partner?id=SWO_550_7A&zone=458", "color": "#d6001e",
            "deposit": "", "freebet": "", "risk_free": "X", "bonus_val": "Bez Podatku"
        },
        {
            "name": "Fuksiarz", "code": "PEWNIAK", "url": "https://fuksem.pl/pewniak", "color": "#c60c30",
            "deposit": "", "freebet": "", "risk_free": "X", "bonus_val": "1000 PLN"
        },
        {
            "name": "Forbet", "code": "PEWNIAKTYPY", "url": "https://www.iforbet.pl/rejestracja/040b9f4849?kod=PEWNIAKTYPY", "color": "#228b22",
            "deposit": "X", "freebet": "X", "risk_free": "X", "bonus_val": "5250 PLN"
        },
        {
            "name": "Betfan", "code": "PEWNIAK", "url": "https://betfan.pl/lp/z4sfbmq4c4", "color": "#009640",
            "deposit": "X", "freebet": "", "risk_free": "", "bonus_val": "200% do 400"
        },
        {
            "name": "Etoto", "code": "PEWNIAK", "url": "https://rejestracja.etoto.pl/?btag=a_965b_417c_&siteid=965", "color": "#0053a0",
            "deposit": "X", "freebet": "", "risk_free": "", "bonus_val": "777 PLN"
        },
        {
            "name": "LVBET", "code": "PEWNIAK", "url": "https://m.lvbetpartners.pl/Redirect.aspx?mid=134&sid=1100&cid=&pid=&affid=656724", "color": "#ffda00", "text_dark": True,
            "deposit": "X", "freebet": "X", "risk_free": "", "bonus_val": "2020 PLN"
        },
        {
            "name": "ComeOn", "code": "PEWNIAK", "url": "https://bit.ly/comeon-pewniak", "color": "#9bf00b", "text_dark": True,
            "deposit": "", "freebet": "", "risk_free": "X", "bonus_val": "100 PLN"
        }
    ]
    return render_template('promocje.html', promos=promos)

@app.route('/analyze/<int:fixture_id>')
def analyze(fixture_id):
    try:
        resp = requests.get(API_URL, headers=HEADERS, params={"ids": fixture_id})
        data = resp.json().get('response', [])
        if not data: return "Brak danych", 404
        match_data = data[0]
        h2h_resp = requests.get(API_URL, headers=HEADERS, params={"h2h": f"{match_data['teams']['home']['id']}-{match_data['teams']['away']['id']}"})
        return render_template('analysis.html', match=match_data, h2h=h2h_resp.json().get('response', []), stats_home={}, stats_away={}, history_home=[], history_away=[])
    except: return "Błąd"

if __name__ == '__main__':
    threading.Thread(target=worker_task, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)