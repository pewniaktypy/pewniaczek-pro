import requests
import pandas as pd
from flask import Flask, render_template, request
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
HEADERS = {
    'x-rapidapi-host': "v3.football.api-sports.io",
    'x-rapidapi-key': API_KEY
}
PL_TIMEZONE = pytz.timezone('Europe/Warsaw')
DB_FILE = "pewniaczek.db"

# KONFIGURACJA CZASÓW (w sekundach)
LIVE_UPDATE_INTERVAL = 120   # Co 2 minuty odświeżamy same wyniki
FULL_DAILY_UPDATE_HOUR = 4   # O której godzinie (rano) robić pełne pobieranie danych na nowy dzień

# --- BAZA DANYCH ---

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    # Tabela meczów (zawiera wszystko: dane meczu + policzone statystyki)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY,
            date_str TEXT,
            sort_priority INTEGER,
            timestamp INTEGER,
            data_json TEXT,
            last_full_update INTEGER
        )
    ''')
    # Tabela pomocnicza na statystyki (cache), żeby nie pobierać tego samego 2x w trakcie "Heavy Job"
    conn.execute('''
        CREATE TABLE IF NOT EXISTS team_stats_cache (
            team_id INTEGER PRIMARY KEY,
            last_10 INTEGER,
            last_6 INTEGER,
            last_3 INTEGER,
            updated_at INTEGER
        )
    ''')
    conn.commit()
    conn.close()

# --- LOGIKA BIZNESOWA (STATYSTYKI) ---

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
    """Pobiera detale dla wielu meczów naraz (oszczędność API)."""
    if not ids_list: return {}
    details_map = {}
    # API pozwala na max 20 ID w jednym zapytaniu
    chunk_size = 20
    chunks = [ids_list[i:i + chunk_size] for i in range(0, len(ids_list), chunk_size)]
    for chunk in chunks:
        ids_str = "-".join(map(str, chunk))
        try:
            resp = requests.get(API_URL, headers=HEADERS, params={"ids": ids_str})
            if resp.status_code == 200:
                for item in resp.json().get('response', []):
                    details_map[item['fixture']['id']] = item
        except: pass
    return details_map

def calculate_team_stats(team_id):
    """Pobiera historię 10 meczów, detale i liczy statystyki."""
    last_10_list = []
    all_fixture_ids = set()
    try:
        # 1. Pobierz listę 10 ostatnich
        resp = requests.get(API_URL, headers=HEADERS, params={"team": team_id, "last": 10})
        if resp.status_code == 200:
            for item in resp.json().get('response', []):
                fixture = item['fixture']
                utc_time = datetime.fromisoformat(fixture['date'].replace('Z', '+00:00'))
                parsed = {
                    'id': fixture['id'],
                    'time_obj': utc_time,
                    'home_id': item['teams']['home']['id'],
                    'away_id': item['teams']['away']['id'],
                }
                last_10_list.append(parsed)
                all_fixture_ids.add(parsed['id'])
    except: return None

    # 2. Pobierz detale (minuty) dla tych meczów
    details_map = get_fixtures_details_bulk(list(all_fixture_ids))
    
    # 3. Policz staty
    matches_list = sorted(last_10_list, key=lambda x: x['time_obj'], reverse=True)
    stats = {'last_10': 0, 'last_6': 0, 'last_3': 0}
    
    for i, m in enumerate(matches_list):
        detail = details_map.get(m['id'])
        if detail and detail.get('events'):
            # Jeśli team_id to gospodarz, to szukamy goli gości (straconych)
            opp_id = m['away_id'] if m['home_id'] == team_id else m['home_id']
            opp_goals = process_goal_minutes(detail['events'], opp_id)
            
            if any(g['is_hot'] for g in opp_goals):
                stats['last_10'] += 1
                if i < 6: stats['last_6'] += 1
                if i < 3: stats['last_3'] += 1
    return stats

def get_or_update_team_stats(team_id):
    """Sprawdza cache w bazie, jak nie ma to pobiera z API."""
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM team_stats_cache WHERE team_id = ?', (team_id,)).fetchone()
    conn.close()

    # Cache ważny 24h
    now_ts = int(time.time())
    if row and (now_ts - row['updated_at'] < 86400):
        return {'last_10': row['last_10'], 'last_6': row['last_6'], 'last_3': row['last_3']}

    # Pobieramy z API
    print(f"[FULL UPDATE] Pobieram historię dla zespołu ID: {team_id}")
    new_stats = calculate_team_stats(team_id)
    
    if new_stats:
        conn = get_db_connection()
        conn.execute('''
            INSERT OR REPLACE INTO team_stats_cache (team_id, last_10, last_6, last_3, updated_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (team_id, new_stats['last_10'], new_stats['last_6'], new_stats['last_3'], now_ts))
        conn.commit()
        conn.close()
        return new_stats
    return None # Błąd API lub brak danych

def get_verdict(stats_home, stats_away):
    verdict = "-"
    verdict_class = "text-secondary"
    if not stats_home or not stats_away: return verdict, verdict_class

    if stats_home['last_3'] >= 3 or stats_away['last_3'] >= 3:
        verdict = "TAK!!"
        verdict_class = "text-success fw-bold"
    elif stats_home['last_6'] >= 3 or stats_away['last_6'] >= 3:
        verdict = "TAK"
        verdict_class = "text-success"
    elif stats_home['last_10'] >= 4 or stats_away['last_10'] >= 4:
        verdict = "MOŻE"
        verdict_class = "text-warning"
    return verdict, verdict_class

def format_score_simple(goals, score, status):
    if status in ['NS', 'TBD']: return "-"
    gh = goals['home'] if goals['home'] is not None else 0
    ga = goals['away'] if goals['away'] is not None else 0
    if score['halftime']['home'] is not None:
        return f"{score['halftime']['home']}-{score['halftime']['away']} / {gh}-{ga}"
    return f"{gh}-{ga}"

# --- GŁÓWNY MECHANIZM WORKERA ---

def perform_full_daily_update():
    """To jest ta CIĘŻKA funkcja. Pobiera wszystko, liczy wszystko."""
    print(">>> [DAILY] Rozpoczynam PEŁNĄ aktualizację dzienną...")
    
    dates = [
        datetime.now(PL_TIMEZONE).strftime('%Y-%m-%d'),
        (datetime.now(PL_TIMEZONE) + timedelta(days=1)).strftime('%Y-%m-%d')
    ]

    for date_str in dates:
        print(f">>> [DAILY] Przetwarzam dzień: {date_str}")
        try:
            # 1. Pobierz listę meczów
            resp = requests.get(API_URL, headers=HEADERS, params={"date": date_str})
            if resp.status_code != 200: continue
            data = resp.json().get('response', [])
            
            # 2. Iteruj i buduj pełne obiekty
            matches_to_save = []
            
            for item in data:
                f = item['fixture']
                # Obliczamy priorytet i czas
                status = f['status']['short']
                prio = 0 if status in ['1H','2H','HT','ET','LIVE'] else (2 if status in ['FT','AET','PEN'] else 1)
                utc_time = datetime.fromisoformat(f['date'].replace('Z', '+00:00'))
                
                # KLUCZOWE: Tutaj pobieramy/liczymy statystyki dla KAŻDEGO meczu
                s_home = get_or_update_team_stats(item['teams']['home']['id'])
                s_away = get_or_update_team_stats(item['teams']['away']['id'])
                
                verdict, v_class = get_verdict(s_home, s_away)
                
                match_obj = {
                    'id': f['id'],
                    'time': utc_time.astimezone(PL_TIMEZONE).strftime('%H:%M'),
                    'league_country': f"{item['league']['name']} ({item['league']['country']})",
                    'match_name': f"{item['teams']['home']['name']} - {item['teams']['away']['name']}",
                    'home_id': item['teams']['home']['id'], 'away_id': item['teams']['away']['id'],
                    'home_name': item['teams']['home']['name'], 'away_name': item['teams']['away']['name'],
                    'formatted_score': format_score_simple(item['goals'], item['score'], status),
                    'status': status, 
                    'is_live': prio == 0,
                    'stat_h': f"{s_home['last_10']}/10" if s_home else "-",
                    'stat_a': f"{s_away['last_10']}/10" if s_away else "-",
                    'verdict': verdict, 'verdict_class': v_class,
                    'h_l10': s_home['last_10'] if s_home else 0, 'h_l6': s_home['last_6'] if s_home else 0, 'h_l3': s_home['last_3'] if s_home else 0,
                    'a_l10': s_away['last_10'] if s_away else 0, 'a_l6': s_away['last_6'] if s_away else 0, 'a_l3': s_away['last_3'] if s_away else 0
                }
                
                matches_to_save.append({
                    'id': f['id'], 'date_str': date_str, 'prio': prio, 'ts': int(utc_time.timestamp()),
                    'json': json.dumps(match_obj)
                })

            # 3. Zapisz wszystko do bazy (nadpisując dzień)
            if matches_to_save:
                conn = get_db_connection()
                conn.execute("DELETE FROM matches WHERE date_str = ?", (date_str,))
                conn.executemany('''
                    INSERT INTO matches (id, date_str, sort_priority, timestamp, data_json, last_full_update)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', [(x['id'], x['date_str'], x['prio'], x['ts'], x['json'], int(time.time())) for x in matches_to_save])
                conn.commit()
                conn.close()
                print(f">>> [DAILY] Zapisano {len(matches_to_save)} meczów dla {date_str}. Pełna analiza.")

        except Exception as e:
            print(f"[DAILY ERROR] {e}")

def perform_live_score_update():
    """To jest LEKKA funkcja. Pobiera tylko wyniki i aktualizuje istniejące rekordy."""
    print("... [LIVE] Sprawdzam wyniki na żywo...")
    date_str = datetime.now(PL_TIMEZONE).strftime('%Y-%m-%d')
    
    try:
        resp = requests.get(API_URL, headers=HEADERS, params={"date": date_str})
        if resp.status_code != 200: return
        data = resp.json().get('response', [])
        
        conn = get_db_connection()
        # Pobieramy obecne dane z bazy, żeby je zaktualizować (a nie nadpisać statystyk!)
        cursor = conn.execute("SELECT id, data_json FROM matches WHERE date_str = ?", (date_str,))
        db_rows = {row['id']: json.loads(row['data_json']) for row in cursor.fetchall()}
        
        updates = []
        for item in data:
            fid = item['fixture']['id']
            if fid in db_rows:
                match_data = db_rows[fid]
                
                # Aktualizujemy TYLKO pola zmienne
                status = item['fixture']['status']['short']
                prio = 0 if status in ['1H','2H','HT','ET','LIVE'] else (2 if status in ['FT','AET','PEN'] else 1)
                
                match_data['formatted_score'] = format_score_simple(item['goals'], item['score'], status)
                match_data['status'] = status
                match_data['is_live'] = (prio == 0)
                
                # Zapisujemy z powrotem
                updates.append((json.dumps(match_data), prio, fid))
        
        if updates:
            conn.executemany("UPDATE matches SET data_json = ?, sort_priority = ? WHERE id = ?", updates)
            conn.commit()
            print(f"... [LIVE] Zaktualizowano wyniki dla {len(updates)} meczów.")
        
        conn.close()

    except Exception as e:
        print(f"[LIVE ERROR] {e}")

def background_worker():
    """Zarządca zadań."""
    print(">>> Uruchamiam Workera.")
    
    # 1. Na starcie: Sprawdź czy mamy dane na dziś. Jak nie - zrób FULL UPDATE.
    conn = get_db_connection()
    today = datetime.now(PL_TIMEZONE).strftime('%Y-%m-%d')
    check = conn.execute("SELECT COUNT(*) FROM matches WHERE date_str = ?", (today,)).fetchone()[0]
    conn.close()
    
    if check == 0:
        print(">>> Brak danych na dziś. Uruchamiam pobieranie początkowe (to potrwa)...")
        perform_full_daily_update()
    else:
        print(">>> Dane na dziś istnieją. Przechodzę do trybu Live.")

    last_live_run = time.time()
    
    while True:
        now = datetime.now(PL_TIMEZONE)
        
        # Codzienny FULL UPDATE o 4:00 rano
        if now.hour == FULL_DAILY_UPDATE_HOUR and now.minute == 0:
             perform_full_daily_update()
             time.sleep(60) # Żeby nie odpalić 2 razy w tej samej minucie

        # LIVE UPDATE co interwał
        if time.time() - last_live_run > LIVE_UPDATE_INTERVAL:
            perform_live_score_update()
            last_live_run = time.time()
            
        time.sleep(10) # Krótka drzemka pętli

# --- ROUTES ---

@app.route('/')
def index():
    now = datetime.now(PL_TIMEZONE)
    today_str = now.strftime('%Y-%m-%d')
    tomorrow_str = (now + timedelta(days=1)).strftime('%Y-%m-%d')
    requested_date = request.args.get('date', today_str)
    current_view = 'tomorrow' if requested_date == tomorrow_str else 'today'

    conn = get_db_connection()
    rows = conn.execute('''
        SELECT data_json FROM matches 
        WHERE date_str = ? 
        ORDER BY sort_priority ASC, timestamp ASC
    ''', (requested_date,)).fetchall()
    conn.close()

    matches_data = [json.loads(row['data_json']) for row in rows]
    return render_template('index.html', matches=pd.DataFrame(matches_data), 
                           today_date=today_str, tomorrow_date=tomorrow_str, current_view=current_view)

@app.route('/analyze/<int:fixture_id>')
def analyze(fixture_id):
    # W analizie nadal chcemy mieć świeże H2H i minutówki, 
    # ale staty główne (Werdykt) bierzemy z bazy (która była zrobiona rano)
    # Chyba że chcemy wymusić naprawę - tutaj zostawiam "self-healing"
    # bo to bezpieczniejsze przy ewentualnych błędach.
    
    home_id = int(request.args.get('home'))
    away_id = int(request.args.get('away'))
    home_name = request.args.get('h_name')
    away_name = request.args.get('a_name')

    # Pobieranie LIVE dla popupu
    h2h_list = []
    home_last = []
    away_last = []
    ids = set()

    def simple_parse(item):
        f = item['fixture']
        t = item['teams']
        g = item['goals']
        d = datetime.fromisoformat(f['date'].replace('Z', '+00:00')).astimezone(PL_TIMEZONE).strftime('%d.%m')
        return {
            'id': f['id'], 'time_obj': datetime.fromisoformat(f['date'].replace('Z', '+00:00')), 'date': d,
            'home_team': t['home']['name'], 'away_team': t['away']['name'], 
            'home_id': t['home']['id'], 'away_id': t['away']['id'],
            'score': f"{g['home']}-{g['away']}" if g['home'] is not None else "-", 
            'league': item['league']['name'], 
            'winner': 'home' if t['home']['winner'] else ('away' if t['away']['winner'] else 'draw')
        }

    try:
        r = requests.get(API_URL + "/headtohead", headers=HEADERS, params={"h2h": f"{home_id}-{away_id}", "last": 10})
        for i in r.json().get('response', []):
            p = simple_parse(i)
            h2h_list.append(p)
            ids.add(p['id'])
    except: pass
    
    for tid, lst in [(home_id, home_last), (away_id, away_last)]:
        try:
            r = requests.get(API_URL, headers=HEADERS, params={"team": tid, "last": 10})
            for i in r.json().get('response', []):
                p = simple_parse(i)
                lst.append(p)
                ids.add(p['id'])
        except: pass
        
    det_map = get_fixtures_details_bulk(list(ids))
    
    def enrich(lst):
        res = []
        for m in lst:
            d = det_map.get(m['id'])
            hg, ag = [], []
            if d and d.get('events'):
                hg = process_goal_minutes(d['events'], m['home_id'])
                ag = process_goal_minutes(d['events'], m['away_id'])
            m['home_goals_events'] = hg
            m['away_goals_events'] = ag
            res.append(m)
        return res

    final_h2h = enrich(h2h_list)
    final_home = enrich(home_last)
    final_away = enrich(away_last)

    # Przeliczamy na świeżo dla Popupu (ale nie nadpisujemy bazy globalnej, żeby nie mieszać w trakcie dnia)
    # Chociaż... jeśli jest błąd w bazie, to warto go naprawić.
    # Włączam naprawianie bazy przy okazji (self-healing).
    
    def calc_live(matches, tid):
        sorted_m = sorted(matches, key=lambda x: x['time_obj'], reverse=True)
        stats = {'last_10': 0, 'last_6': 0, 'last_3': 0}
        for i, m in enumerate(sorted_m):
            opp = m['away_goals_events'] if m['home_id'] == tid else m['home_goals_events']
            if any(g['is_hot'] for g in opp):
                stats['last_10']+=1
                if i<6: stats['last_6']+=1
                if i<3: stats['last_3']+=1
        return stats

    s_home = calc_live(final_home, home_id)
    s_away = calc_live(final_away, away_id)
    
    # Update cache (naprawa)
    conn = get_db_connection()
    ts = int(time.time())
    conn.execute("INSERT OR REPLACE INTO team_stats_cache (team_id, last_10, last_6, last_3, updated_at) VALUES (?,?,?,?,?)", 
                 (home_id, s_home['last_10'], s_home['last_6'], s_home['last_3'], ts))
    conn.execute("INSERT OR REPLACE INTO team_stats_cache (team_id, last_10, last_6, last_3, updated_at) VALUES (?,?,?,?,?)", 
                 (away_id, s_away['last_10'], s_away['last_6'], s_away['last_3'], ts))
    conn.commit()
    conn.close()

    verdict, v_class = get_verdict(s_home, s_away)

    return render_template('analysis.html', 
        h2h=final_h2h, home_last=final_home, away_last=final_away,
        home_name=home_name, away_name=away_name, 
        stats_home=s_home, stats_away=s_away,
        verdict=verdict, verdict_class=v_class, current_score="?")

if __name__ == '__main__':
    if not os.path.exists(DB_FILE): init_db()
    t = threading.Thread(target=background_worker)
    t.daemon = True
    t.start()
    app.run(host='0.0.0.0', port=5000, debug=True)
