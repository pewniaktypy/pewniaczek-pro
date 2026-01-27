import requests
import sqlite3
import json
import time
import sys
import os
from datetime import datetime

# --- KONFIGURACJA ---
try:
    from config import API_KEY
except ImportError:
    API_KEY = os.environ.get("API_KEY", "BRAK_KLUCZA")

DB_FILE = "pewniaczek.db"
API_LEAGUES_URL = "https://v3.football.api-sports.io/leagues" # <-- Nowy endpoint do szukania lig
API_TEAMS_URL = "https://v3.football.api-sports.io/teams"
API_FIXTURES_URL = "https://v3.football.api-sports.io/fixtures"
HEADERS = {'x-rapidapi-host': "v3.football.api-sports.io", 'x-rapidapi-key': API_KEY}

# --- 1. KRAJE DO PRZESKANOWANIA (Pobierze WSZYSTKIE ligi dla tych krajów) ---
TARGET_COUNTRIES = [
    "India",      # Indie (Pobierze wszystko co jest: ISL, I-League itd.)
    "Indonesia",  # Indonezja (Liga 1, Liga 2...)
    "Thailand",   # Tajlandia (Thai League 1, 2...)
    "Japan",      # Japonia (J1, J2, J3)
    "South Korea",# Korea Płd
    "China",      # Chiny
    "Saudi Arabia",
    "Iran",
    "Vietnam",
    "Malaysia",
    "Uzbekistan",
    "Singapore",
    "Qatar",
    "United Arab Emirates"
]

# --- 2. DODATKOWE KONKRETNE ID (Dla Młodzieżówek - żeby nie pobierać seniorów) ---
# Anglia i Holandia mają za dużo lig seniorskich, więc tu podajemy ID ręcznie dla precyzji
YOUTH_LEAGUE_IDS = [
    865, 866, # Anglia: Premier League 2 (Div 1 & 2)
    757,      # Anglia: U18 Premier League
    264,      # Holandia: U21 Divisie 1
    608,      # Holandia: U21 Divisie 2 (jeśli jest)
]

SEASONS_HISTORY = [2020, 2021, 2022, 2023, 2024, 2025]

def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS analyzed_teams (
                    team_id INTEGER PRIMARY KEY, name TEXT, league_name TEXT, 
                    total_matches INTEGER, total_lamaks INTEGER, percent REAL, 
                    jan_all_matches INTEGER, jan_all_lamaks INTEGER, jan_curr_lamaks INTEGER, 
                    jan_streak INTEGER, month_streak INTEGER, next_match_date TEXT, 
                    next_opponent TEXT, last_updated TEXT, smart_stats_json TEXT)''')
    conn.commit(); conn.close()

# --- BEZPIECZNE ZAPYTANIE ---
def safe_request(url, params):
    retries = 0
    while retries < 3:
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=20)
            if r.status_code == 429:
                print("\n[!] Limit API (429). Pauza 60s...")
                time.sleep(60)
                retries += 1
                continue
            if r.status_code != 200: return None
            return r.json()
        except:
            time.sleep(2); retries += 1
    return None

# --- AUTOMATYCZNE ZNAJDOWANIE ID LIG PO KRAJU ---
def find_leagues_by_country(country_name):
    print(f"🔍 Szukam lig dla kraju: {country_name}...")
    data = safe_request(API_LEAGUES_URL, {"country": country_name, "type": "league"}) # Tylko ligi, bez pucharów
    
    found_ids = []
    if data and data.get('response'):
        for item in data['response']:
            league = item['league']
            # Pobieramy ID
            lid = league['id']
            lname = league['name']
            print(f"   -> Znaleziono: [{lid}] {lname}")
            found_ids.append(lid)
    else:
        print(f"   -> [X] Brak lig dla {country_name}")
    
    return found_ids

# --- ANALIZA DRUŻYNY ---
def analyze_team(team_id, team_name, country):
    all_matches = []
    for season in SEASONS_HISTORY:
        data = safe_request(API_FIXTURES_URL, {"team": team_id, "season": season, "status": "FT"})
        if data and data.get('response'): all_matches.extend(data['response'])
    
    if not all_matches: return None

    TARGET_MONTH = 1; CURRENT_YEAR = 2026
    total_m = 0; total_l = 0
    jan_all_matches = 0; jan_all_lamaks = 0; jan_curr_lamaks = 0
    lamaki_months_set = set(); seen = set()

    for m in all_matches:
        if m['fixture']['id'] in seen: continue
        seen.add(m['fixture']['id'])
        ht = m['score']['halftime']; ft = m['score']['fulltime']
        if ht.get('home') is None or ft.get('home') is None: continue
        
        total_m += 1; is_rev = False
        if (ht['home'] > ht['away'] and ft['home'] < ft['away']) or (ht['away'] > ht['home'] and ft['home'] > ft['away']): is_rev = True; total_l += 1
            
        try:
            m_date = datetime.fromisoformat(m['fixture']['date'].replace('Z',''))
            if m_date.month == TARGET_MONTH:
                jan_all_matches += 1
                if is_rev: jan_all_lamaks += 1
                if m_date.year == CURRENT_YEAR and is_rev: jan_curr_lamaks += 1
            if is_rev: lamaki_months_set.add((m_date.year, m_date.month))
        except: pass

    perc = round((total_l / total_m) * 100, 1) if total_m > 0 else 0
    
    jan_streak = 0
    for y in range(CURRENT_YEAR - 1, 2019, -1):
        if (y, TARGET_MONTH) in lamaki_months_set: jan_streak += 1
        else: break
    month_streak = 0; cy = CURRENT_YEAR; cm = 12 
    while True:
        if cy < 2020: break
        if (cy, cm) in lamaki_months_set: month_streak += 1; cm -= 1; 
        if cm == 0: cm = 12; cy -= 1
        else: break

    today_str = datetime.now().strftime('%Y-%m-%d')
    conn = get_db()
    existing = conn.execute("SELECT smart_stats_json FROM analyzed_teams WHERE team_id=?", (team_id,)).fetchone()
    smart_json = existing['smart_stats_json'] if existing else None

    conn.execute('''INSERT OR REPLACE INTO analyzed_teams 
                    (team_id, name, league_name, total_matches, total_lamaks, percent, 
                    jan_all_matches, jan_all_lamaks, jan_curr_lamaks, jan_streak, month_streak, last_updated, smart_stats_json) 
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', 
                 (team_id, team_name, f"{country}", total_m, total_l, perc, 
                  jan_all_matches, jan_all_lamaks, jan_curr_lamaks, jan_streak, month_streak, today_str, smart_json))
    conn.commit(); conn.close()
    return True

# --- GŁÓWNA PĘTLA ---
def run_seeder():
    print("--- START SMART SEEDER (Country Auto-Discovery) ---")
    init_db()
    
    # 1. Zbieranie ID lig
    final_league_ids = list(YOUTH_LEAGUE_IDS) # Zaczynamy od młodzieżówek
    
    print(f"\n--- FAZA 1: Wyszukiwanie lig dla {len(TARGET_COUNTRIES)} krajów ---")
    for country in TARGET_COUNTRIES:
        ids = find_leagues_by_country(country)
        final_league_ids.extend(ids)
        time.sleep(0.5) # Oddech dla API
        
    # Usuwamy duplikaty
    final_league_ids = list(set(final_league_ids))
    print(f"\n✅ Znaleziono łącznie {len(final_league_ids)} lig do pobrania.")
    
    # 2. Pobieranie drużyn
    print(f"\n--- FAZA 2: Pobieranie i analiza drużyn ---")
    
    for i, lid in enumerate(final_league_ids):
        print(f"\n[{i+1}/{len(final_league_ids)}] Liga ID: {lid}...")
        
        # Smart Season Check (2025 -> 2024 -> 2023)
        teams = []
        for s in [2025, 2024, 2023]:
            data = safe_request(API_TEAMS_URL, {"league": lid, "season": s})
            if data and data.get('response'):
                teams = [t['team'] for t in data['response']]
                print(f"   -> Znaleziono {len(teams)} drużyn w sezonie {s}")
                break
        
        if not teams:
            print("   -> [X] Brak drużyn/sezonu. Pomijam.")
            continue
            
        # Analiza
        count = 0
        for t in teams:
            count += 1
            conn = get_db()
            exists = conn.execute("SELECT 1 FROM analyzed_teams WHERE team_id=? AND last_updated=?", (t['id'], datetime.now().strftime('%Y-%m-%d'))).fetchone()
            conn.close()
            
            if exists:
                sys.stdout.write(".")
                sys.stdout.flush()
                continue
            
            sys.stdout.write("#")
            sys.stdout.flush()
            analyze_team(t['id'], t['name'], t['country'])
            
    print("\n\n--- ZAKOŃCZONO! Baza jest pełna. ---")

if __name__ == "__main__":
    run_seeder()