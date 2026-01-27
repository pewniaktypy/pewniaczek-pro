import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import pytz
import time
import threading
import sqlite3
import json
import os
import sys

try:
    from config import API_KEY
except ImportError:
    API_KEY = os.environ.get("API_KEY", "BRAK_KLUCZA")

app = Flask(__name__)

# CONFIG
SAFE_DELAY = 0.1
API_FIXTURES_URL = "https://v3.football.api-sports.io/fixtures"
API_TEAMS_URL = "https://v3.football.api-sports.io/teams"
HEADERS = {'x-rapidapi-host': "v3.football.api-sports.io", 'x-rapidapi-key': API_KEY}
PL_TIMEZONE = pytz.timezone('Europe/Warsaw')
DB_FILE = "pewniaczek.db"
DB_PATH = os.path.join(os.getcwd(), DB_FILE)

# SEZONY DO SKANOWANIA
curr_year = datetime.now().year
SEASONS_TO_SCAN = [curr_year - i for i in range(6)] 

LEAGUE_IDS = [39, 40, 41, 42, 43, 140, 141, 78, 79, 80, 135, 136, 137, 61, 62, 63, 106, 107, 108, 88, 89, 94, 95]

progress_status = {"message": "System ONLINE"}

def get_session():
    session = requests.Session()
    retry = Retry(connect=3, read=3, backoff_factor=0.5)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

def safe_request(url, params):
    session = get_session()
    while True:
        try:
            time.sleep(SAFE_DELAY)
            resp = session.get(url, headers=HEADERS, params=params, timeout=10)
            if resp.status_code == 200: return resp.json()
            if resp.status_code == 429:
                print("⚠️ Limit API. Pauza 5s..."); time.sleep(5); continue
            return None
        except: time.sleep(1)

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=60) 
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    try:
        conn = get_db_connection()
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA synchronous=NORMAL;')
        conn.execute('CREATE TABLE IF NOT EXISTS matches (id INTEGER PRIMARY KEY, date_str TEXT, timestamp INTEGER, data_json TEXT)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_date ON matches(date_str)')
        conn.execute('CREATE TABLE IF NOT EXISTS favorites (fixture_id INTEGER PRIMARY KEY)')
        conn.execute('''CREATE TABLE IF NOT EXISTS analyzed_teams (
                        team_id INTEGER PRIMARY KEY, name TEXT, league_name TEXT, 
                        total_matches INTEGER, total_lamaks INTEGER, percent REAL, 
                        jan_all_matches INTEGER, jan_all_lamaks INTEGER, jan_curr_lamaks INTEGER, 
                        jan_streak INTEGER, month_streak INTEGER, next_match_date TEXT, 
                        next_opponent TEXT, last_updated TEXT, smart_stats_json TEXT)''')
        conn.commit(); conn.close()
    except Exception as e: print(f"DB Error: {e}")

init_db()

def get_match_score_string(goals, halftime, status):
    if status in ['NS', 'TBD']: return "-"
    gh = goals.get('home'); ga = goals.get('away')
    if gh is None: return "-" 
    ft_str = f"{gh}-{ga}"
    hh = halftime.get('home'); ha = halftime.get('away')
    if hh is not None and ha is not None: return f"({hh}-{ha}) {ft_str}"
    return f"(?-?) {ft_str}"

def process_goal_minutes(events, team_id):
    if not events: return False
    for event in events:
        if event.get('type') == 'Goal' and event.get('team', {}).get('id') == team_id:
            m = event.get('time', {}).get('elapsed', 0)
            if (40 <= m <= 45) or (85 <= m <= 90): return True
    return False

# --- NOWA FUNKCJA: ANALIZA GŁĘBOKA + ZWRACANIE LISTY MECZÓW ---
def analyze_history_5years_DEEP(team_id, team_name, country, today_str, conn):
    all_matches = []
    for s in SEASONS_TO_SCAN:
        d = safe_request(API_FIXTURES_URL, {"team": team_id, "season": s, "status": "FT"})
        if d: all_matches.extend(d.get('response', []))
    
    total_m = 0; total_l = 0; jan_all = 0; jan_l = 0; jan_years = set()
    lamaki_months_set = set()

    for m in all_matches:
        ht = m['score']['halftime']; ft = m['score']['fulltime']
        if not ht.get('home') or not ft.get('home'): continue
        total_m += 1; is_rev = False
        if (ht['home'] > ht['away'] and ft['home'] < ft['away']) or (ht['away'] > ht['home'] and ft['home'] > ft['away']):
            is_rev = True; total_l += 1
        try:
            dt = datetime.fromisoformat(m['fixture']['date'].replace('Z',''))
            if dt.month == 1:
                jan_all += 1
                if is_rev: jan_l += 1; jan_years.add(dt.year)
            if is_rev: lamaki_months_set.add((dt.year, dt.month))
        except: pass
        
    perc = round((total_l / total_m) * 100, 1) if total_m > 0 else 0
    
    curr_y = datetime.now(PL_TIMEZONE).year
    jan_streak = 0
    if curr_y not in jan_years: curr_y -= 1
    while curr_y in jan_years: jan_streak += 1; curr_y -= 1
    
    month_streak = 0
    now = datetime.now(PL_TIMEZONE); y, m = now.year, now.month
    for _ in range(24): 
        m -= 1; 
        if m == 0: m = 12; y -= 1
        if (y, m) in lamaki_months_set: month_streak += 1
        else: break

    conn.execute('''INSERT OR REPLACE INTO analyzed_teams VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', 
                 (team_id, team_name, country, total_m, total_l, perc, jan_all, jan_l, 0, jan_streak, month_streak, None, None, today_str, json.dumps({'last_10':0}))) 
    conn.commit()

    stats = {
        'percent': perc, 'total': total_m, 
        'jan_lamaks': jan_l, 'jan_matches': jan_all, 
        'jan_streak': jan_streak, 'month_streak': month_streak
    }
    return stats, all_matches

def analyze_form_last10(team_id, conn):
    today_str = datetime.now(PL_TIMEZONE).strftime('%Y-%m-%d')
    row = conn.execute("SELECT smart_stats_json, last_updated FROM analyzed_teams WHERE team_id=?", (team_id,)).fetchone()
    if row and row['smart_stats_json'] and row['last_updated'] == today_str: return json.loads(row['smart_stats_json'])
    
    data = safe_request(API_FIXTURES_URL, {"team": team_id, "last": 15, "status": "FT"})
    if not data: return None # ZWRACAMY NONE JEŚLI API PADŁO

    matches = data.get('response', []); filtered = []
    for m in matches:
        if 'Friendly' not in m['league'].get('name', ''): filtered.append(m)
        if len(filtered) >= 10: break
    stats = {'last_10': 0}
    if filtered:
        ids = "-".join([str(m['fixture']['id']) for m in filtered])
        det_data = safe_request(API_FIXTURES_URL, {"ids": ids})
        if det_data:
            details = det_data.get('response', [])
            det_map = {d['fixture']['id']: d for d in details}
            for m in filtered:
                d = det_map.get(m['fixture']['id'])
                if d and d.get('events'):
                    opp_id = d['teams']['away']['id'] if d['teams']['home']['id'] == team_id else d['teams']['home']['id']
                    if process_goal_minutes(d['events'], opp_id): stats['last_10'] += 1
    
    conn.execute("INSERT OR IGNORE INTO analyzed_teams (team_id, name, league_name) VALUES (?,?,?)", (team_id, "Unknown", "Unknown"))
    conn.execute("UPDATE analyzed_teams SET smart_stats_json = ?, last_updated = ? WHERE team_id = ?", (json.dumps(stats), today_str, team_id))
    conn.commit()
    return stats

# --- WORKER Z NAPRAWIONĄ DEKLARACJĄ ZMIENNYCH ---
def worker_task():
    print("--- ROBOT LIVE START (FIXED VARS) ---")
    while True:
        try:
            init_db()
            now = datetime.now(PL_TIMEZONE)
            dates_to_scan = [
                now.strftime('%Y-%m-%d'),
                (now + timedelta(days=1)).strftime('%Y-%m-%d')
            ]

            # 1. FAZA SZYBKA: INIT
            for d in dates_to_scan:
                data = safe_request(API_FIXTURES_URL, {"date": d})
                if not data: continue
                fixtures = data.get('response', [])
                conn = get_db_connection(); conn.execute('BEGIN TRANSACTION;')
                for item in fixtures:
                    fid = item['fixture']['id']
                    if not conn.execute("SELECT 1 FROM matches WHERE id=?", (fid,)).fetchone():
                        m_obj = {
                            'id': fid, 'time': datetime.fromisoformat(item['fixture']['date'].replace('Z','+00:00')).astimezone(PL_TIMEZONE).strftime('%H:%M'),
                            'league_country': f"{item['league']['name']} ({item['league']['country']})",
                            'match_name': f"{item['teams']['home']['name']} - {item['teams']['away']['name']}",
                            'home_name': item['teams']['home']['name'], 'away_name': item['teams']['away']['name'],
                            'score': "-", 'status': item['fixture']['status']['short'], 'is_live': False,
                            'lamak_h': "...", 'lamak_a': "...", 'tag': "", 'verdict': "-", 'verdict_class': "",
                            'h_total': 0, 'h_m_hist': "-/-", 'a_total': 0, 'a_m_hist': "-/-", 'h_str_y': 0, 'a_str_y': 0, 'h_str_m': 0, 'a_str_m': 0,
                            'stat_h': "0/10", 'stat_a': "0/10", 'h_l10': 0, 'a_l10': 0, 'h2h_lamak': "-", 'goal_minutes': "", 'formatted_score': "-"
                        }
                        conn.execute('INSERT INTO matches (id, date_str, timestamp, data_json) VALUES (?,?,?,?)', (fid, d, item['fixture']['timestamp'], json.dumps(m_obj)))
                conn.commit(); conn.close()
            
            # 2. FAZA ANALIZY (POPRAWIONA)
            for d in dates_to_scan:
                conn = get_db_connection()
                rows = conn.execute("SELECT id, data_json FROM matches WHERE date_str=?", (d,)).fetchall()
                conn.close()
                
                data = safe_request(API_FIXTURES_URL, {"date": d})
                if not data: continue
                fixtures_map = {f['fixture']['id']: f for f in data.get('response', [])}

                for row in rows:
                    fid = row['id']
                    item = fixtures_map.get(fid)
                    if not item: continue

                    m_obj = json.loads(row['data_json'])
                    upd = False
                    
                    # --- TU BYŁ BŁĄD! DEFINIUJEMY ID ZAWSZE NA POCZĄTKU ---
                    h_id = item['teams']['home']['id']
                    a_id = item['teams']['away']['id']
                    # -------------------------------------------------------

                    # Aktualizacja LIVE/Wyniku
                    sc = get_match_score_string(item['goals'], item['score']['halftime'], item['fixture']['status']['short'])
                    if m_obj.get('score') != sc:
                        m_obj['score'] = sc; m_obj['formatted_score'] = sc
                        m_obj['status'] = item['fixture']['status']['short']
                        m_obj['is_live'] = item['fixture']['status']['short'] in ['1H','2H','HT','LIVE','ET']
                        tag = ""
                        ht=item['score']['halftime']; ft=item['score']['fulltime']
                        if ht.get('home') and ft.get('home'):
                            if (ht['home']>ht['away'] and ft['home']<ft['away']) or (ht['away']>ht['home'] and ft['home']>ft['away']): tag="TAK"
                        m_obj['tag'] = tag; upd = True

                    # GŁÓWNA ANALIZA 5 LAT + H2H
                    if m_obj.get('lamak_h') == "...":
                        today_update = datetime.now(PL_TIMEZONE).strftime('%Y-%m-%d')
                        
                        t_conn = get_db_connection()
                        s_h, matches_h = analyze_history_5years_DEEP(h_id, item['teams']['home']['name'], item['league']['country'], today_update, t_conn)
                        s_a, matches_a = analyze_history_5years_DEEP(a_id, item['teams']['away']['name'], item['league']['country'], today_update, t_conn)
                        t_conn.close()
                        
                        if s_h:
                            m_obj['lamak_h'] = f"{s_h['percent']}%"; m_obj['h_total'] = s_h['total']
                            m_obj['h_m_hist'] = f"{s_h['jan_lamaks']}/{s_h['jan_matches']}"
                            m_obj['h_str_y'] = s_h['jan_streak']; m_obj['h_str_m'] = s_h['month_streak']
                        if s_a:
                            m_obj['lamak_a'] = f"{s_a['percent']}%"; m_obj['a_total'] = s_a['total']
                            m_obj['a_m_hist'] = f"{s_a['jan_lamaks']}/{s_a['jan_matches']}"
                            m_obj['a_str_y'] = s_a['jan_streak']; m_obj['a_str_m'] = s_a['month_streak']
                        
                        # H2H LOKALNIE
                        h2h_matches = []
                        if matches_h:
                            for m in matches_h:
                                if m['teams']['home']['id'] == a_id or m['teams']['away']['id'] == a_id:
                                    h2h_matches.append(m)
                        
                        total_h2h = len(h2h_matches)
                        if total_h2h > 0:
                            rev_count = 0
                            for m in h2h_matches:
                                ht = m['score']['halftime']; ft = m['score']['fulltime']
                                if ht.get('home') and ft.get('home'):
                                    if (ht['home']>ht['away'] and ft['home']<ft['away']) or (ht['away']>ht['home'] and ft['home']>ft['away']):
                                        rev_count += 1
                            m_obj['h2h_lamak'] = f"{rev_count}/{total_h2h}"
                        else:
                            m_obj['h2h_lamak'] = "0/0"
                        upd = True

                    # LAST 10 FORM
                    if m_obj.get('stat_h') == "0/10":
                        t_conn = get_db_connection()
                        f_h = analyze_form_last10(h_id, t_conn)
                        f_a = analyze_form_last10(a_id, t_conn)
                        t_conn.close()
                        
                        # Jeśli API zwróciło dane (nie jest None)
                        if f_h and f_a:
                            verdict = "-"; v_class = ""
                            if f_h['last_10'] >= 4 or f_a['last_10'] >= 4: verdict = "MOŻE"; v_class = "text-warning"
                            if f_h['last_10'] >= 5 or f_a['last_10'] >= 5: verdict = "TAK"; v_class = "text-success"
                            m_obj.update({'stat_h': f"{f_h['last_10']}/10", 'stat_a': f"{f_a['last_10']}/10", 'h_l10': f_h['last_10'], 'a_l10': f_a['last_10'], 'verdict': verdict, 'verdict_class': v_class})
                            upd = True
                        else:
                            # API PADŁO - Nie aktualizujemy, zostawiamy 0/10 na kolejną próbę
                            pass

                    if upd:
                        u_conn = get_db_connection() 
                        u_conn.execute('UPDATE matches SET data_json=? WHERE id=?', (json.dumps(m_obj), fid)); u_conn.commit()
                        u_conn.close()
            
            time.sleep(60)
        except Exception as e: print(f"Err Worker: {e}"); time.sleep(60)

def teams_db_worker():
    time.sleep(10); session = get_session()
    while True:
        try:
            init_db(); conn = get_db_connection()
            for lid in LEAGUE_IDS:
                resp = session.get(API_TEAMS_URL, headers=HEADERS, params={"league": lid, "season": 2024}, timeout=15)
                teams = [t['team'] for t in resp.json().get('response', [])]
                for t in teams:
                    if conn.execute("SELECT 1 FROM analyzed_teams WHERE team_id=?", (t['id'],)).fetchone(): continue
                    analyze_history_5years_DEEP(t['id'], t['name'], t['country'], "", conn)
            conn.close(); time.sleep(43200)
        except: time.sleep(60)

# --- ROUTES ---
@app.route('/')
def index():
    now_pl = datetime.now(PL_TIMEZONE); date_str = request.args.get('date', now_pl.strftime('%Y-%m-%d'))
    conn = get_db_connection(); conn.execute('CREATE TABLE IF NOT EXISTS favorites (fixture_id INTEGER PRIMARY KEY)')
    rows = conn.execute('SELECT data_json FROM matches WHERE date_str = ? ORDER BY timestamp ASC', (date_str,)).fetchall()
    favs = {r[0] for r in conn.execute("SELECT fixture_id FROM favorites").fetchall()}; conn.close()
    matches = [dict(json.loads(r['data_json']), is_favorite=(json.loads(r['data_json'])['id'] in favs)) for r in rows]
    matches.sort(key=lambda x: (not x['is_favorite'], x['time']))
    return render_template('index.html', matches=pd.DataFrame(matches), today_date=now_pl.strftime('%Y-%m-%d'), tomorrow_date=(now_pl + timedelta(days=1)).strftime('%Y-%m-%d'), current_view=date_str)

@app.route('/lamaki')
def lamaki():
    now_pl = datetime.now(PL_TIMEZONE); date_str = request.args.get('date', now_pl.strftime('%Y-%m-%d'))
    conn = get_db_connection(); conn.execute('CREATE TABLE IF NOT EXISTS favorites (fixture_id INTEGER PRIMARY KEY)')
    rows = conn.execute('SELECT data_json FROM matches WHERE date_str = ? ORDER BY timestamp ASC', (date_str,)).fetchall()
    favs = {r[0] for r in conn.execute("SELECT fixture_id FROM favorites").fetchall()}; conn.close()
    matches_list = []
    for r in rows:
        m = json.loads(r['data_json'])
        m['is_favorite'] = m['id'] in favs
        matches_list.append(m)
    matches_list.sort(key=lambda x: (not x['is_favorite'], x['time']))
    return render_template('lamaki.html', matches=pd.DataFrame(matches_list), today_date=now_pl.strftime('%Y-%m-%d'), tomorrow_date=(now_pl + timedelta(days=1)).strftime('%Y-%m-%d'), current_view=date_str, curr_month="Styczeń")

@app.route('/teams')
def teams_view():
    conn = get_db_connection()
    teams = conn.execute("SELECT * FROM analyzed_teams ORDER BY percent DESC").fetchall()
    leagues = sorted([r[0] for r in conn.execute("SELECT DISTINCT league_name FROM analyzed_teams").fetchall() if r[0]])
    conn.close()
    return render_template('teams.html', teams=teams, leagues=leagues)

@app.route('/toggle_favorite/<int:fixture_id>', methods=['POST'])
def toggle_favorite(fixture_id):
    try:
        conn = get_db_connection(); conn.execute('CREATE TABLE IF NOT EXISTS favorites (fixture_id INTEGER PRIMARY KEY)')
        if conn.execute("SELECT 1 FROM favorites WHERE fixture_id = ?", (fixture_id,)).fetchone():
            conn.execute("DELETE FROM favorites WHERE fixture_id = ?", (fixture_id,)); f=False
        else: conn.execute("INSERT INTO favorites (fixture_id) VALUES (?)", (fixture_id,)); f=True
        conn.commit(); conn.close(); return jsonify({"success": True, "is_favorite": f})
    except: return jsonify({"success": False})

@app.route('/promocje')
def promocje(): return render_template('promocje.html', promos=[{"name": "Superbet", "url": "#", "color": "#ef2f2f", "bonus_val": "3754 PLN"}])

@app.route('/analyze/<int:fixture_id>')
def analyze(fixture_id):
    try:
        session = requests.Session(); resp = session.get(f"{API_FIXTURES_URL}?ids={fixture_id}", headers=HEADERS)
        data = resp.json().get('response', [])
        if not data: return f"Błąd: Brak meczu ID {fixture_id} w API"
        md = data[0]
        h_id = md['teams']['home']['id']; a_id = md['teams']['away']['id']
        h2h_raw = safe_request(API_FIXTURES_URL, {"h2h": f"{h_id}-{a_id}"})
        h2h = []
        if h2h_raw:
            for m in h2h_raw.get('response', []):
                ht=m['score']['halftime']; ft=m['score']['fulltime']
                rev_class = ""
                if ht.get('home') and ft.get('home'):
                    if (ht['home']>ht['away'] and ft['home']<ft['away']) or (ht['away']>ht['home'] and ft['home']>ft['away']): rev_class = "text-danger fw-bold"
                h2h.append({'date':m['fixture']['date'][:10], 'league': m['league']['name'], 'home_name': m['teams']['home']['name'], 'away_name': m['teams']['away']['name'], 'score': f"{m['goals']['home']}-{m['goals']['away']}", 'rev_class': rev_class})

        def get_10_with_minutes(tid):
            h=[]
            data = safe_request(API_FIXTURES_URL, {"team": tid, "last": 10, "status": "FT"})
            if data and data.get('response'):
                matches = data['response']
                ids = "-".join([str(m['fixture']['id']) for m in matches])
                det = safe_request(API_FIXTURES_URL, {"ids": ids})
                det_map = {d['fixture']['id']: d for d in det.get('response', [])} if det else {}
                for m in matches:
                    fid = m['fixture']['id']; goals_events = []
                    if fid in det_map and det_map[fid].get('events'):
                        for e in det_map[fid]['events']:
                            if e.get('type') == 'Goal':
                                t = str(e['time']['elapsed']); ex=e['time'].get('extra')
                                if ex: t+=f"+{ex}"
                                goals_events.append({'time': t, 'team_id': e['team']['id']})
                    h.append({'date': m['fixture']['date'][:10], 'home_name': m['teams']['home']['name'], 'home_id': m['teams']['home']['id'], 'away_name': m['teams']['away']['name'], 'away_id': m['teams']['away']['id'], 'score': f"{m['goals']['home']}-{m['goals']['away']}", 'ht_score': f"({m['score']['halftime']['home']}-{m['score']['halftime']['away']})", 'goals': goals_events})
            return h
        return render_template('analysis.html', match=md, h2h=h2h, history_home=get_10_with_minutes(h_id), history_away=get_10_with_minutes(a_id))
    except Exception as e: return f"Błąd analizy: {e}"

@app.route('/analyze_lamaki/<int:fixture_id>')
def analyze_lamaki(fixture_id):
    try:
        session = requests.Session(); resp = session.get(f"{API_FIXTURES_URL}?ids={fixture_id}", headers=HEADERS)
        data = resp.json().get('response', [])
        md = data[0]
        h_id = md['teams']['home']['id']; a_id = md['teams']['away']['id']
        h2h_raw = safe_request(API_FIXTURES_URL, {"h2h": f"{h_id}-{a_id}"})
        h2h = []
        if h2h_raw:
            for m in h2h_raw.get('response', []):
                ht=m['score']['halftime']; ft=m['score']['fulltime']
                is_lamak = False
                if ht.get('home') and ft.get('home'):
                    if (ht['home']>ht['away'] and ft['home']<ft['away']) or (ht['away']>ht['home'] and ft['home']>ft['away']): is_lamak = True
                h2h.append({'date':m['fixture']['date'][:10], 'league':m['league']['name'], 'home_name':m['teams']['home']['name'], 'away_name':m['teams']['away']['name'], 'score':f"{m['goals']['home']}-{m['goals']['away']}", 'ht_score': f"{ht['home']}-{ht['away']}" if ht.get('home') else "?-?", 'is_lamak': is_lamak})
        
        def get_rev_only(tid):
            h=[]
            for s in SEASONS_TO_SCAN:
                d = safe_request(API_FIXTURES_URL, {"team": tid, "season": s, "status": "FT"})
                if d:
                    for m in d.get('response',[]):
                        ht=m['score']['halftime']; ft=m['score']['fulltime']
                        if not ht.get('home') or not ft.get('home'): continue
                        ir=False; tr=""
                        if (ht['home']>ht['away'] and ft['home']<ft['away']): ir=True; tr="1/2"
                        elif (ht['away']>ht['home'] and ft['home']>ft['away']): ir=True; tr="2/1"
                        if ir: h.append({'date':m['fixture']['date'][:10],'league':m['league']['name'],'home_name':m['teams']['home']['name'],'away_name':m['teams']['away']['name'],'score':f"{ft['home']}-{ft['away']}",'type':tr})
            return h
        return render_template('analysis_lamaki.html', match=md, h2h=h2h, reversals_home=get_rev_only(h_id), reversals_away=get_rev_only(a_id))
    except Exception as e: return f"Błąd: {e}"

@app.route('/progress')
def get_progress(): return jsonify(progress_status)

if __name__ == '__main__':
    threading.Thread(target=worker_task, daemon=True).start()
    threading.Thread(target=teams_db_worker, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)