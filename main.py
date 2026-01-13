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
API_KEY = "a9c00564195a59b4641909f9610c378d"
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
    if not events: return []
    goals = []
    for event in events:
        if event.get('type') == 'Goal' and event.get('team', {}).get('id') == team_id:
            m = event.get('time', {}).get('elapsed', 0)
            ex = event.get('time', {}).get('extra')
            is_hot = (40 <= m <= 45) or (85 <= m <= 90)
            goals.append({'time': f"{m}+{ex}" if ex else f"{m}", 'is_hot': is_hot})
    return goals

def calculate_smart_stats(team_id, target_type):
    try:
        time.sleep(0.05) 
        resp = requests.get(API_URL, headers=HEADERS, params={"team": team_id, "last": 40})
        if resp.status_code != 200: return {'last_10': 0, 'last_6': 0, 'last_3': 0}
            
        all_matches = resp.json().get('response', [])
        if not all_matches: return {'last_10': 0, 'last_6': 0, 'last_3': 0}

        filtered = []
        for m in all_matches:
            l_info = m.get('league', {})
            # --- ZABEZPIECZENIE (TU BYŁ BŁĄD) ---
            m_type = l_info.get('type', 'League') 
            m_name = l_info.get('name', '')
            is_f = 'Friendly' in m_name or 'Club Friendlies' in m_name
            
            if target_type == 'Friendly' and is_f: filtered.append(m)
            elif target_type == 'Cup' and m_type == 'Cup' and not is_f: filtered.append(m)
            elif target_type == 'League' and m_type == 'League' and not is_f: filtered.append(m)
            
            if len(filtered) >= 10: break
            
        if not filtered: return {'last_10': 0, 'last_6': 0, 'last_3': 0}

        stats = {'last_10': 0, 'last_6': 0, 'last_3': 0}
        for i, m in enumerate(filtered):
            fid = m['fixture']['id']
            det_resp = requests.get(API_URL, headers=HEADERS, params={"id": fid})
            det_data = det_resp.json().get('response', [])
            
            if det_data and det_data[0].get('events'):
                opp_id = m['teams']['away']['id'] if m['teams']['home']['id'] == team_id else m['teams']['home']['id']
                if any(g['is_hot'] for g in process_goal_minutes(det_data[0]['events'], opp_id)):
                    stats['last_10'] += 1
                    if i < 6: stats['last_6'] += 1
                    if i < 3: stats['last_3'] += 1
        return stats
    except Exception:
        return {'last_10': 0, 'last_6': 0, 'last_3': 0}

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
                
                resp = requests.get(API_URL, headers=HEADERS, params={"date": date_str})
                if resp.status_code != 200: continue
                    
                data = resp.json().get('response', [])
                progress_status["total"] = len(data)
                
                conn = get_db_connection()
                
                for i, item in enumerate(data):
                    try:
                        progress_status["current"] = i + 1
                        h_name = item['teams']['home']['name']
                        progress_status["message"] = f"Analiza ({i+1}/{len(data)}): {h_name}"

                        f = item['fixture']
                        l_info = item.get('league', {})
                        
                        # --- ZABEZPIECZENIE PRZED KEYERROR ---
                        l_type_api = l_info.get('type', 'League')
                        l_name_api = l_info.get('name', '')
                        
                        if 'Friendly' in l_name_api: m_type = 'Friendly'
                        elif l_type_api == 'Cup': m_type = 'Cup'
                        else: m_type = 'League'
                        
                        s_h = calculate_smart_stats(item['teams']['home']['id'], m_type)
                        s_a = calculate_smart_stats(item['teams']['away']['id'], m_type)
                        
                        v, vc = "-", "text-secondary"
                        if s_h['last_3'] >= 3 or s_a['last_3'] >= 3: v, vc = "TAK!!", "text-success fw-bold"
                        elif s_h['last_6'] >= 3 or s_a['last_6'] >= 3: v, vc = "TAK", "text-success"
                        elif s_h['last_10'] >= 4 or s_a['last_10'] >= 4: v, vc = "MOŻE", "text-warning"

                        m_obj = {
                            'id': f['id'],
                            'time': datetime.fromisoformat(f['date'].replace('Z','+00:00')).astimezone(PL_TIMEZONE).strftime('%H:%M'),
                            'league_country': f"{l_name_api} ({l_info.get('country','-')})",
                            'match_name': f"{item['teams']['home']['name']} - {item['teams']['away']['name']}",
                            'stat_h': f"{s_h['last_10']}/10", 'stat_a': f"{s_a['last_10']}/10",
                            'verdict': v, 'verdict_class': vc,
                            'status': f['status']['short'],
                            'is_live': f['status']['short'] in ['1H','2H','HT','LIVE','ET'],
                            'h_l10': s_h['last_10'], 'h_l6': s_h['last_6'], 'h_l3': s_h['last_3'],
                            'a_l10': s_a['last_10'], 'a_l6': s_a['last_6'], 'a_l3': s_a['last_3'],
                            'formatted_score': f"{item['goals']['home']}-{item['goals']['away']}" if item['goals']['home'] is not None else "vs"
                        }
                        
                        conn.execute('INSERT OR REPLACE INTO matches (id, date_str, timestamp, data_json) VALUES (?,?,?,?)', 
                                     (f['id'], date_str, f['timestamp'], json.dumps(m_obj)))
                        conn.commit()
                        
                    except Exception: continue

                conn.close()
        except Exception: pass
            
        progress_status["is_running"] = False
        time.sleep(600)

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
    return render_template('index.html', matches=pd.DataFrame(matches), 
                           today_date=now_pl.strftime('%Y-%m-%d'), 
                           tomorrow_date=(now_pl + timedelta(days=1)).strftime('%Y-%m-%d'),
                           current_view=date_str)

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
        data = resp.json().get('response', [])
        if not data: return "Brak danych", 404
        match_data = data[0]
        h2h_resp = requests.get(API_URL, headers=HEADERS, params={"h2h": f"{match_data['teams']['home']['id']}-{match_data['teams']['away']['id']}"})
        sh = get_detailed_stats_for_analysis(match_data['teams']['home']['id'])
        sa = get_detailed_stats_for_analysis(match_data['teams']['away']['id'])
        return render_template('analysis.html', match=match_data, h2h=h2h_resp.json().get('response', []), 
                             stats_home=sh, stats_away=sa, history_home=sh['matches'], history_away=sa['matches'])
    except Exception as e: return f"Błąd: {e}"

if __name__ == '__main__':
    threading.Thread(target=worker_task, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
