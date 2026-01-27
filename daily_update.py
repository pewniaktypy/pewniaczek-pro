import requests
import sqlite3
import time
import json
import os
from datetime import datetime
import sys
import pytz  # <--- To było potrzebne

# --- KONFIGURACJA ---
try:
    from config import API_KEY
except ImportError:
    API_KEY = os.environ.get("API_KEY", "BRAK_KLUCZA")

API_FIXTURES_URL = "https://v3.football.api-sports.io/fixtures"
HEADERS = {'x-rapidapi-host': "v3.football.api-sports.io", 'x-rapidapi-key': API_KEY}
DB_FILE = "pewniaczek.db"
RATE_LIMIT_SLEEP = 0.15  # Bezpieczna prędkość
SEASONS_TO_SCAN = [2021, 2022, 2023, 2024, 2025]
PL_TIMEZONE = pytz.timezone('Europe/Warsaw') # <--- TEGO BRAKOWAŁO

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def safe_request(url, params):
    while True:
        try:
            time.sleep(RATE_LIMIT_SLEEP)
            resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
            if resp.status_code == 200: return resp.json()
            if resp.status_code == 429:
                log("⚠️ Limit API (429). Czekam 60s...")
                time.sleep(60); continue
            return None
        except Exception as e:
            log(f"Błąd sieci: {e}")
            time.sleep(5)

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('CREATE TABLE IF NOT EXISTS matches (id INTEGER PRIMARY KEY, date_str TEXT, timestamp INTEGER, data_json TEXT)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_date ON matches(date_str)')
    conn.execute('CREATE TABLE IF NOT EXISTS analyzed_teams (team_id INTEGER PRIMARY KEY, name TEXT, league_name TEXT, total_matches INTEGER, total_lamaks INTEGER, percent REAL, jan_all_matches INTEGER, jan_all_lamaks INTEGER, jan_curr_lamaks INTEGER, jan_streak INTEGER, month_streak INTEGER, next_match_date TEXT, next_opponent TEXT, last_updated TEXT, smart_stats_json TEXT)')
    conn.commit()
    return conn

def process_goal_minutes(events, team_id):
    if not events: return False
    for event in events:
        if event.get('type') == 'Goal' and event.get('team', {}).get('id') == team_id:
            m = event.get('time', {}).get('elapsed', 0)
            if (40 <= m <= 45) or (85 <= m <= 90): return True
    return False

def get_match_score_string(goals, halftime, status):
    if status in ['NS', 'TBD']: return "-"
    if status in ['PST', 'CANC', 'ABD']: return status
    gh = goals.get('home'); ga = goals.get('away')
    if gh is None: return "-" 
    ft_str = f"{gh}-{ga}"
    hh = halftime.get('home'); ha = halftime.get('away')
    if hh is not None and ha is not None: return f"({hh}-{ha}) {ft_str}"
    else: return f"(?-?) {ft_str}"

def run_daily_update():
    conn = init_db()
    today = datetime.now().strftime('%Y-%m-%d')
    log(f"--- ROZPOCZYNAM POBIERANIE DANYCH NA DZIEŃ: {today} ---")
    log("Krok 1: Pobieranie listy meczów...")

    data = safe_request(API_FIXTURES_URL, {"date": today})
    if not data: log("Błąd pobierania listy meczów."); return

    fixtures = data.get('response', [])
    total_fixtures = len(fixtures)
    log(f"Znaleziono {total_fixtures} meczów. Rozpoczynam szczegółową analizę...")

    # Pobierz ID meczów do detali
    all_ids = [m['fixture']['id'] for m in fixtures]
    detailed_fixtures_map = {}
    
    # Pobieranie detali w paczkach po 20
    chunk_size = 20
    for i in range(0, len(all_ids), chunk_size):
        chunk = all_ids[i:i+chunk_size]
        ids_str = "-".join(map(str, chunk))
        det = safe_request(API_FIXTURES_URL, {"ids": ids_str})
        if det:
            for d in det.get('response', []):
                detailed_fixtures_map[d['fixture']['id']] = d
        print(f"\rPobieranie detali: {min(i+chunk_size, len(all_ids))}/{len(all_ids)}", end="")
        sys.stdout.flush()
    print("\nDetale pobrane. Analiza historii i H2H...")

    count = 0
    for simple_fixture in fixtures:
        count += 1
        fid = simple_fixture['fixture']['id']
        fixture = detailed_fixtures_map.get(fid, simple_fixture)
        
        h_id = fixture['teams']['home']['id']; h_name = fixture['teams']['home']['name']
        a_id = fixture['teams']['away']['id']; a_name = fixture['teams']['away']['name']
        country = fixture['league'].get('country', 'World')

        print(f"[{count}/{total_fixtures}] Analiza: {h_name} vs {a_name}...", end=" ")
        sys.stdout.flush()

        # 1. ANALIZA ŁAMAKÓW (5 Lat)
        row_h = conn.execute("SELECT last_updated FROM analyzed_teams WHERE team_id=?", (h_id,)).fetchone()
        
        def analyze_5_years(tid, tname):
            all_m = []
            for s in SEASONS_TO_SCAN:
                d = safe_request(API_FIXTURES_URL, {"team": tid, "season": s, "status": "FT"})
                if d: all_m.extend(d.get('response', []))
            
            total_m = 0; total_l = 0; jan_all_matches = 0; jan_all_lamaks = 0
            TARGET_MONTH = 1
            for m in all_m:
                ht = m['score']['halftime']; ft = m['score']['fulltime']
                if not ht.get('home') or not ft.get('home'): continue
                total_m += 1
                is_rev = False
                if (ht['home'] > ht['away'] and ft['home'] < ft['away']) or (ht['away'] > ht['home'] and ft['home'] > ft['away']):
                    is_rev = True; total_l += 1
                
                try:
                    dt = datetime.fromisoformat(m['fixture']['date'].replace('Z',''))
                    if dt.month == TARGET_MONTH:
                        jan_all_matches += 1
                        if is_rev: jan_all_lamaks += 1
                except: pass
            
            perc = round((total_l / total_m) * 100, 1) if total_m > 0 else 0
            
            conn.execute('''INSERT OR REPLACE INTO analyzed_teams VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', 
                 (tid, tname, country, total_m, total_l, perc, jan_all_matches, jan_all_lamaks, 0, 0, 0, None, None, today, json.dumps({'last_10':0})))
            return {'percent': perc, 'jan_all_lamaks': jan_all_lamaks, 'jan_all_matches': jan_all_matches}

        if not row_h or row_h[0] != today: s_h = analyze_5_years(h_id, h_name)
        else: 
            rh_data = conn.execute("SELECT percent, jan_all_lamaks, jan_all_matches FROM analyzed_teams WHERE team_id=?", (h_id,)).fetchone()
            s_h = {'percent': rh_data[0], 'jan_all_lamaks': rh_data[1], 'jan_all_matches': rh_data[2]}

        row_a = conn.execute("SELECT last_updated FROM analyzed_teams WHERE team_id=?", (a_id,)).fetchone()
        if not row_a or row_a[0] != today: s_a = analyze_5_years(a_id, a_name)
        else:
            ra_data = conn.execute("SELECT percent, jan_all_lamaks, jan_all_matches FROM analyzed_teams WHERE team_id=?", (a_id,)).fetchone()
            s_a = {'percent': ra_data[0], 'jan_all_lamaks': ra_data[1], 'jan_all_matches': ra_data[2]}

        # 2. H2H
        h2h_data = safe_request(API_FIXTURES_URL, {"h2h": f"{h_id}-{a_id}", "last": 40})
        has_h2h_lamak = False
        if h2h_data:
            for m in h2h_data.get('response', []):
                ht = m['score']['halftime']; ft = m['score']['fulltime']
                if ht.get('home') is not None and ft.get('home') is not None:
                    if (ht['home'] > ht['away'] and ft['home'] < ft['away']) or (ht['away'] > ht['home'] and ft['home'] > ft['away']):
                        has_h2h_lamak = True; break
        
        # 3. ZAPIS DO TABELI MATCHES
        goal_minutes_list = []
        has_late_goal = False
        if fixture.get('events'):
            for e in fixture['events']:
                if e.get('type') == 'Goal':
                    t = e['time']['elapsed']
                    extra = e['time'].get('extra')
                    time_str = f"{t}+{extra}'" if extra else f"{t}'"
                    goal_minutes_list.append(time_str)
                    if t >= 85: has_late_goal = True

        tag = ""
        ht = fixture['score']['halftime']; ft = fixture['score']['fulltime']
        if ht.get('home') is not None and ft.get('home') is not None:
             if (ht['home'] > ht['away'] and ft['home'] < ft['away']) or (ht['away'] > ht['home'] and ft['home'] > ft['away']): tag = "TAK"

        verdict = "-" # Werdykt "MOŻE/TAK" obliczy main.py na podstawie G10, tu wpisujemy -
        
        m_obj = {
            'id': fid, 
            'time': datetime.fromisoformat(fixture['fixture']['date'].replace('Z','+00:00')).astimezone(PL_TIMEZONE).strftime('%H:%M'),
            'league_country': f"{fixture['league'].get('name')} ({country})",
            'match_name': f"{h_name} - {a_name}", 
            'home_name': h_name, 'away_name': a_name,
            'score': get_match_score_string(fixture['goals'], fixture['score']['halftime'], fixture['fixture']['status']['short']),
            'status': fixture['fixture']['status']['short'],
            'is_live': fixture['fixture']['status']['short'] in ['1H','2H','HT','LIVE','ET'],
            'lamak_h': f"{s_h['percent']}%", 
            'lamak_a': f"{s_a['percent']}%",
            'tag': tag,
            'h_total': 0, # Te pola są mniej istotne w tym widoku
            'h_m_hist': f"{s_h['jan_all_lamaks']}/{s_h['jan_all_matches']}",
            'h_str_y': 0, 'h_str_m': 0,
            'a_total': 0, 
            'a_m_hist': f"{s_a['jan_all_lamaks']}/{s_a['jan_all_matches']}", 
            'a_str_y': 0, 'a_str_m': 0,
            'stat_h': "0/10", 'stat_a': "0/10", 
            'verdict': verdict, 'verdict_class': "",
            'late_goal': has_late_goal, 
            'h2h_lamak': has_h2h_lamak,
            'goal_minutes': ", ".join(goal_minutes_list)
        }
        
        conn.execute('INSERT OR REPLACE INTO matches (id, date_str, timestamp, data_json) VALUES (?,?,?,?)', 
                     (fid, today, fixture['fixture']['timestamp'], json.dumps(m_obj)))
        conn.commit()
        print("OK")

    conn.close()
    log("=== ZAKOŃCZONO POBIERANIE DZIENNE ===")
    log("Teraz możesz uruchomić main.py")

if __name__ == '__main__':
    run_daily_update()