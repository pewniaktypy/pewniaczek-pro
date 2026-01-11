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
    # Usuwamy tabelę cache statystyk przy starcie, bo logika się zmieniła i stare dane by mieszały
    conn.execute('DROP TABLE IF EXISTS team_stats_cache') 
    conn.execute('CREATE TABLE IF NOT EXISTS team_stats_cache (team_id INTEGER, match_type TEXT, last_10 INTEGER, last_6 INTEGER, last_3 INTEGER, updated_at INTEGER, PRIMARY KEY (team_id, match_type))')
    conn.commit()
    conn.close()

# --- LOGIKA BIZNESOWA ---
def process_goal_minutes(events, team_id):
    goals = []
    if not events: return []
    for event in events:
        if event['type'] == 'Goal' and event['team']['id'] == team_id:
            minute = event['time']['elapsed']
            extra = event['time']['extra']
            is_hot = (40 <= minute <= 45) or (85 <= minute <= 90)
            min_str = f"{minute}+{extra}" if extra else f"{minute}"
            goals.append({'time': min_str, 'is_hot': is_hot})
    return goals

def get_fixtures_details_bulk(ids_list):
    if not ids_list: return {}
    details_map = {}
    chunk_size = 20
    for i in range(0, len(ids_list), chunk_size):
        ids_str = "-".join(map(str, ids_list[i:i + chunk_size]))
        try:
            resp = requests.get(API_URL, headers=HEADERS, params={"ids": ids_str})
            if resp.status_code == 200:
                for item in resp.json().get('response', []):
                    details_map[item['fixture']['id']] = item
        except: pass
    return details_map

def calculate_smart_stats(team_id, target_type):
    """
    Inteligentna funkcja:
    target_type: 'League', 'Cup', lub 'Friendly'
    Pobiera 40 ostatnich meczów i filtruje 10 pasujących do typu.
    """
    try:
        # Pobieramy więcej (40), żeby znaleźć 10 pasujących
        resp = requests.get(API_URL, headers=HEADERS, params={"team": team_id, "last": 40})
        if resp.status_code != 200: return None
        all_matches = resp.json().get('response', [])
        
        filtered_matches = []
        for m in all_matches:
            m_league_type = m['league']['type'] # 'League' lub 'Cup'
            m_league_name = m['league']['name']
            
            is_friendly = 'Friendly' in m_league_name or 'Club Friendlies' in m_league_name
            
            if target_type == 'Friendly':
                if is_friendly: filtered_matches.append(m)
            elif target_type == 'Cup':
                if m_league_type == 'Cup' and not is_friendly: filtered_matches.append(m)
            else: # League (Domyślnie)
                if m_league_type == 'League' and not is_friendly: filtered_matches.append(m)
                
            if len(filtered_matches) >= 10: break # Mamy komplet
        
        # Jeśli nie znaleziono 10 meczów danego typu (np. mało pucharów), bierzemy co jest
        # Jeśli pusto w pucharach, ewentualnie można wziąć ogólne, ale trzymajmy się rygoru
        
        ids = [m['fixture']['id'] for m in filtered_matches]
        if not ids: return {'last_10': 0, 'last_6': 0, 'last_3': 0} # Brak danych historycznych
        
        det_map = get_fixtures_details_bulk(ids)
        stats = {'last_10': 0, 'last_6': 0, 'last_3': 0}
        
        for i, m in enumerate(filtered_matches):
            fid = m['fixture']['id']
            detail = det_map.get(fid)
            if detail and detail.get('events'):
                opp_id = m['teams']['away']['id'] if m['teams']['home']['id'] == team_id else m['teams']['home']['id']
                opp_goals = process_goal_minutes(detail['events'], opp_id)
                if any(g['is_hot'] for g in opp_goals):
                    stats['last_10'] += 1
                    if i < 6: stats['last_6'] += 1
                    if i < 3: stats['last_3'] += 1
        return stats
    except: return None

def update_db_stats(team_id, match_type, stats):
    conn = get_db_connection()
    conn.execute('INSERT OR REPLACE INTO team_stats_cache (team_id, match_type, last_10, last_6, last_3, updated_at) VALUES (?,?,?,?,?,?)',
                 (team_id, match_type, stats['last_10'], stats['last_6'], stats['last_3'], int(time.time())))
    conn.commit()
    conn.close()

def get_cached_stats(team_id, match_type):
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM team_stats_cache WHERE team_id = ? AND match_type = ?', (team_id, match_type)).fetchone()
    conn.close()
    if row and (int(time.time()) - row['updated_at'] < 86400):
        return {'last_10': row['last_10'], 'last_6': row['last_6'], 'last_3': row['last_3']}
    return None

def get_verdict(sh, sa):
    if not sh or not sa: return "-", "text-secondary"
    if sh['last_3'] >= 3 or sa['last_3'] >= 3: return "TAK!!", "text-success fw-bold"
    if sh['last_6'] >= 3 or sa['last_6'] >= 3: return "TAK", "text-success"
    if sh['last_10'] >= 4 or sa['last_10'] >= 4: return "MOŻE", "text-warning"
    return "-", "text-secondary"

# --- WORKER ---
def perform_full_update_for_date(date_str):
    global progress_status
    progress_status["message"] = f"Pobieranie danych na {date_str}..."
    progress_status["is_running"] = True
    try:
        resp = requests.get(API_URL, headers=HEADERS, params={"date": date_str})
        data = resp.json().get('response', [])
        if not data: return
        progress_status["total"] = len(data)
        progress_status["current"] = 0
        conn = get_db_connection()
        conn.execute("DELETE FROM matches WHERE date_str = ?", (date_str,))
        conn.commit()
        
        for i, item in enumerate(data):
            progress_status["current"] = i + 1
            f = item['fixture']
            h_id, a_id = item['teams']['home']['id'], item['teams']['away']['id']
            
            # OKREŚLANIE TYPU MECZU
            league_type = item['league']['type'] # 'League' lub 'Cup'
            league_name = item['league']['name']
            match_type = 'League'
            if 'Friendly' in league_name or 'Club Friendlies' in league_name:
                match_type = 'Friendly'
            elif league_type == 'Cup':
                match_type = 'Cup'
            
            # Pobieranie statystyk pod konkretny typ
            s_h = get_cached_stats(h_id, match_type) or calculate_smart_stats(h_id, match_type)
            if s_h: update_db_stats(h_id, match_type, s_h)
            
            s_a = get_cached_stats(a_id, match_type) or calculate_smart_stats(a_id, match_type)
            if s_a: update_db_stats(a_id, match_type, s_a)
            
            v, vc = get_verdict(s_h, s_a)
            status = f['status']['short']
            is_live = status in ['1H','2H','HT','ET','LIVE']
            prio = 0 if is_live else (2 if status in ['FT','AET','PEN'] else 1)
            score_str = "-"
            if item['goals']['home'] is not None: score_str = f"{item['goals']['home']}-{item['goals']['away']}"
            
            m_obj = {
                'id': f['id'], 'time': datetime.fromisoformat(f['date'].replace('Z','+00:00')).astimezone(PL_TIMEZONE).strftime('%H:%M'),
                'league_country': f"{item['league']['name']} ({item['league']['country']})",
                'match_name': f"{item['teams']['home']['name']} - {item['teams']['away']['name']}",
                'home_id': h_id, 'away_id': a_id, 'home_name': item['teams']['home']['name'], 'away_name': item['teams']['away']['name'],
                'formatted_score': score_str, 'status': status, 'is_live': is_live,
                'stat_h': f"{s_h['last_10']}/10" if s_h else "-", 'stat_a': f"{s_a['last_10']}/10" if s_a else "-",
                'verdict': v, 'verdict_class': vc,
                'match_type_display': match_type, # Do wyświetlenia w debugu lub tooltipie
                'h_l10': s_h['last_10'] if s_h else 0, 'h_l6': s_h['last_6'] if s_h else 0, 'h_l3': s_h['last_3'] if s_h else 0,
                'a_l10': s_a['last_10'] if s_a else 0, 'a_l6': s_a['last_6'] if s_a else 0, 'a_l3': s_a['last_3'] if s_a else 0
            }
            conn.execute('INSERT INTO matches (id, date_str, sort_priority, timestamp, data_json, last_full_update) VALUES (?,?,?,?,?,?)',
                         (f['id'], date_str, prio, int(datetime.fromisoformat(f['date'].replace('Z','+00:00')).timestamp()), json.dumps(m_obj), int(time.time())))
            conn.commit()
        conn.close()
    except Exception as e: pass
    finally: progress_status["is_running"] = False

def update_live_scores(date_str):
    try:
        resp = requests.get(API_URL, headers=HEADERS, params={"date": date_str})
        if resp.status_code != 200: return
        data = resp.json().get('response', [])
        conn = get_db_connection()
        cursor = conn.execute("SELECT id, data_json FROM matches WHERE date_str = ?", (date_str,))
        db_rows = {row['id']: json.loads(row['data_json']) for row in cursor.fetchall()}
        updates = []
        for item in data:
            fid = item['fixture']['id']
            if fid in db_rows:
                m_data = db_rows[fid]
                status = item['fixture']['status']['short']
                if item['goals']['home'] is not None: m_data['formatted_score'] = f"{item['goals']['home']}-{item['goals']['away']}"
                m_data['status'] = status
                m_data['is_live'] = status in ['1H','2H','HT','ET','LIVE']
                prio = 0 if m_data['is_live'] else (2 if status in ['FT','AET','PEN'] else 1)
                updates.append((json.dumps(m_data), prio, fid))
        if updates:
            conn.executemany("UPDATE matches SET data_json = ?, sort_priority = ? WHERE id = ?", updates)
            conn.commit()
        conn.close()
    except: pass

def background_worker():
    while True:
        with app.app_context():
            today = datetime.now(PL_TIMEZONE).strftime('%Y-%m-%d')
            tomorrow = (datetime.now(PL_TIMEZONE) + timedelta(days=1)).strftime('%Y-%m-%d')
            conn = get_db_connection()
            c_today = conn.execute("SELECT COUNT(*) FROM matches WHERE date_str = ?", (today,)).fetchone()[0]
            c_tomorrow = conn.execute("SELECT COUNT(*) FROM matches WHERE date_str = ?", (tomorrow,)).fetchone()[0]
            conn.close()
            if c_today == 0: perform_full_update_for_date(today)
            if c_tomorrow == 0: perform_full_update_for_date(tomorrow)
            update_live_scores(today)
        time.sleep(120)

@app.route('/progress')
def get_progress(): return jsonify(progress_status)

@app.route('/')
def index():
    now = datetime.now(PL_TIMEZONE)
    date_str = request.args.get('date', now.strftime('%Y-%m-%d'))
    conn = get_db_connection()
    rows = conn.execute('SELECT data_json FROM matches WHERE date_str = ? ORDER BY sort_priority ASC, timestamp ASC', (date_str,)).fetchall()
    conn.close()
    matches = [json.loads(r['data_json']) for r in rows]
    return render_template('index.html', matches=pd.DataFrame(matches), today_date=now.strftime('%Y-%m-%d'), tomorrow_date=(now + timedelta(days=1)).strftime('%Y-%m-%d'), current_view=date_str)

def get_detailed_stats(team_id, matches_count=5):
    try:
        resp = requests.get(API_URL, headers=HEADERS, params={"team": team_id, "last": matches_count})
        if resp.status_code != 200: return {}
        matches = resp.json().get('response', [])
        wins = 0; draws = 0; losses = 0; goals_for = 0; goals_against = 0
        for m in matches:
            home = m['teams']['home']['id'] == team_id
            g_f = m['goals']['home'] if home else m['goals']['away']
            g_a = m['goals']['away'] if home else m['goals']['home']
            if g_f is None: g_f = 0
            if g_a is None: g_a = 0
            goals_for += g_f; goals_against += g_a
            if g_f > g_a: wins += 1
            elif g_f == g_a: draws += 1
            else: losses += 1
        return {'matches': matches, 'wins': wins, 'draws': draws, 'losses': losses, 'goals_for': goals_for, 'goals_against': goals_against}
    except: return {}

@app.route('/analyze/<int:fixture_id>')
def analyze(fixture_id):
    try:
        resp = requests.get(API_URL, headers=HEADERS, params={"ids": fixture_id})
        if resp.status_code != 200: return "Błąd API (Match Details)", 500
        data = resp.json().get('response', [])
        if not data: return "Nie znaleziono meczu", 404
        match_data = data[0]
        h_id = match_data['teams']['home']['id']
        a_id = match_data['teams']['away']['id']
        h2h_resp = requests.get(API_URL, headers=HEADERS, params={"h2h": f"{h_id}-{a_id}"})
        h2h = h2h_resp.json().get('response', []) if h2h_resp.status_code == 200 else []
        stats_home = get_detailed_stats(h_id, 5)
        stats_away = get_detailed_stats(a_id, 5)
        return render_template('analysis.html', fixture_id=fixture_id, match=match_data, h2h=h2h, stats_home=stats_home, stats_away=stats_away)
    except Exception as e: return f"Błąd krytyczny podczas analizy: {str(e)}", 500

if __name__ == '__main__':
    init_db()
    t = threading.Thread(target=background_worker)
    t.daemon = True
    t.start()
    app.run(host='0.0.0.0', port=5000, debug=False)
