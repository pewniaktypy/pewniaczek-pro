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

app = Flask(__name__)

# --- KONFIGURACJA ---
API_URL = "https://v3.football.api-sports.io/fixtures"
API_KEY = "a9c00564195a59b4641909f9610c378d"
HEADERS = {'x-rapidapi-host': "v3.football.api-sports.io", 'x-rapidapi-key': API_KEY}
PL_TIMEZONE = pytz.timezone('Europe/Warsaw')
DB_FILE = "pewniaczek.db"
progress_status = {"total": 0, "current": 0, "is_running": False, "message": ""}

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS matches (id INTEGER PRIMARY KEY, date_str TEXT, sort_priority INTEGER, timestamp INTEGER, data_json TEXT, last_full_update INTEGER)')
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

def calculate_stats(team_id):
    try:
        time.sleep(0.1)
        resp = requests.get(API_URL, headers=HEADERS, params={"team": team_id, "last": 10})
        if resp.status_code != 200: return {'last_10': 0, 'last_6': 0, 'last_3': 0}
        matches = resp.json().get('response', [])
        stats = {'last_10': 0, 'last_6': 0, 'last_3': 0}
        for i, m in enumerate(matches):
            fid = m['fixture']['id']
            det = requests.get(API_URL, headers=HEADERS, params={"id": fid}).json().get('response', [])
            if det and det[0].get('events'):
                opp_id = m['teams']['away']['id'] if m['teams']['home']['id'] == team_id else m['teams']['home']['id']
                if any(g['is_hot'] for g in process_goal_minutes(det[0]['events'], opp_id)):
                    stats['last_10'] += 1
                    if i < 6: stats['last_6'] += 1
                    if i < 3: stats['last_3'] += 1
        return stats
    except: return {'last_10': 0, 'last_6': 0, 'last_3': 0}

def worker_task():
    global progress_status
    init_db()
    while True:
        today = datetime.now(PL_TIMEZONE).strftime('%Y-%m-%d')
        progress_status["is_running"] = True
        try:
            resp = requests.get(API_URL, headers=HEADERS, params={"date": today})
            data = resp.json().get('response', [])
            progress_status["total"] = len(data)
            conn = get_db_connection()
            for i, item in enumerate(data):
                progress_status["current"] = i + 1
                progress_status["message"] = f"Analiza: {item['teams']['home']['name']}"
                f = item['fixture']
                s_h = calculate_stats(item['teams']['home']['id'])
                s_a = calculate_stats(item['teams']['away']['id'])
                
                # Werdykt
                v, vc = "-", "text-secondary"
                if (s_h['last_3'] >= 3) or (s_a['last_3'] >= 3): v, vc = "TAK!!", "text-success fw-bold"
                elif (s_h['last_6'] >= 3) or (s_a['last_6'] >= 3): v, vc = "TAK", "text-success"
                elif (s_h['last_10'] >= 4) or (s_a['last_10'] >= 4): v, vc = "MOŻE", "text-warning"

                m_obj = {
                    'id': f['id'], 'time': f['date'][11:16],
                    'league_country': f"{item['league']['name']} ({item['league']['country']})",
                    'match_name': f"{item['teams']['home']['name']} - {item['teams']['away']['name']}",
                    'stat_h': f"{s_h['last_10']}/10", 'stat_a': f"{s_a['last_10']}/10",
                    'verdict': v, 'verdict_class': vc, 'status': f['status']['short'],
                    'h_l10': s_h['last_10'], 'h_l6': s_h['last_6'], 'h_l3': s_h['last_3'],
                    'a_l10': s_a['last_10'], 'a_l6': s_a['last_6'], 'a_l3': s_a['last_3'],
                    'formatted_score': f"{item['goals']['home']}-{item['goals']['away']}" if item['goals']['home'] is not None else "vs"
                }
                conn.execute('INSERT OR REPLACE INTO matches (id, date_str, timestamp, data_json) VALUES (?,?,?,?)', 
                             (f['id'], today, f['timestamp'], json.dumps(m_obj)))
                conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error: {e}")
        progress_status["is_running"] = False
        time.sleep(600)

@app.route('/progress')
def get_progress(): return jsonify(progress_status)

@app.route('/')
def index():
    today = datetime.now(PL_TIMEZONE).strftime('%Y-%m-%d')
    conn = get_db_connection()
    rows = conn.execute('SELECT data_json FROM matches WHERE date_str = ?', (today,)).fetchall()
    conn.close()
    matches = [json.loads(r['data_json']) for r in rows]
    # Sortowanie po czasie dla frontendu
    matches.sort(key=lambda x: x['time'])
    return render_template('index.html', matches=pd.DataFrame(matches), today_date=today)

def get_detailed_stats(team_id, count=5):
    try:
        resp = requests.get(API_URL, headers=HEADERS, params={"team": team_id, "last": count})
        matches = resp.json().get('response', [])
        history = []
        w, d, l, gf, ga = 0, 0, 0, 0, 0
        for m in matches:
            home = m['teams']['home']['id'] == team_id
            f = m['goals']['home'] if home else m['goals']['away']
            a = m['goals']['away'] if home else m['goals']['home']
            gf += (f or 0); ga += (a or 0)
            res = 'draw'
            if f > a: res = 'home' if home else 'away'; w += 1
            elif f < a: l += 1
            else: d += 1
            history.append({
                'home_team': m['teams']['home']['name'], 'away_team': m['teams']['away']['name'],
                'score': f"{m['goals']['home']}-{m['goals']['away']}", 'winner': res
            })
        return {'matches': history, 'wins': w, 'draws': d, 'losses': l, 'goals_for': gf, 'goals_against': ga}
    except: return {'matches': [], 'wins': 0, 'draws': 0, 'losses': 0, 'goals_for': 0, 'goals_against': 0}

@app.route('/analyze/<int:fixture_id>')
def analyze(fixture_id):
    resp = requests.get(API_URL, headers=HEADERS, params={"id": fixture_id})
    data = resp.json().get('response', [])[0]
    h2h_resp = requests.get(API_URL, headers=HEADERS, params={"h2h": f"{data['teams']['home']['id']}-{data['teams']['away']['id']}"})
    sh = get_detailed_stats(data['teams']['home']['id'])
    sa = get_detailed_stats(data['teams']['away']['id'])
    return render_template('analysis.html', match=data, h2h=h2h_resp.json().get('response', []), 
                         stats_home=sh, stats_away=sa, history_home=sh['matches'], history_away=sa['matches'])

if __name__ == '__main__':
    threading.Thread(target=worker_task, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
