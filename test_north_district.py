import requests
import json
import sys

# --- KONFIGURACJA ---
try:
    from config import API_KEY
except ImportError:
    API_KEY = "BRAK_KLUCZA" # Tu robot wezmie z configu jesli istnieje

HEADERS = {
    'x-rapidapi-host': "v3.football.api-sports.io",
    'x-rapidapi-key': API_KEY
}

SZUKANA = "North District"

print(f"--- DIAGNOSTYKA: {SZUKANA} ---")

# 1. Znajdź ID
url_search = "https://v3.football.api-sports.io/teams"
resp = requests.get(url_search, headers=HEADERS, params={"search": SZUKANA})
data_team = resp.json().get('response', [])

if not data_team:
    print("❌ Nie znaleziono takiej drużyny w API!")
    sys.exit()

team_id = data_team[0]['team']['id']
team_name = data_team[0]['team']['name']
print(f"✅ Znaleziono: {team_name} (ID: {team_id})")

# 2. Pobierz historię
print("📊 Pobieram 50 ostatnich meczów...")
url_fixtures = "https://v3.football.api-sports.io/fixtures"
params = {"team": team_id, "last": 50, "status": "FT"}
resp_fix = requests.get(url_fixtures, headers=HEADERS, params=params)
matches = resp_fix.json().get('response', [])

if not matches:
    print("⚠️ API zwróciło 0 meczów historii dla tej drużyny.")
    sys.exit()

lamaki = 0
with_ht = 0

print("-" * 40)
for m in matches[:10]: # Pokaż 10 ostatnich
    d = m['fixture']['date'][:10]
    h = m['teams']['home']['name']
    a = m['teams']['away']['name']
    s = m['score']
    ht = s['halftime']
    ft = s['fulltime']
    
    if ht['home'] is None:
        print(f"{d} | {h} vs {a} | ❌ BRAK DANYCH HT")
    else:
        with_ht += 1
        # Sprawdzam łamaka
        is_rev = False
        # 1/2 lub 2/1
        if (ht['home'] > ht['away'] and ft['home'] < ft['away']) or            (ht['away'] > ht['home'] and ft['away'] < ft['home']):
           is_rev = True
           lamaki += 1
        
        info = "🔥 ŁAMAK!" if is_rev else "Brak"
        print(f"{d} | HT {ht['home']}-{ht['away']} -> FT {ft['home']}-{ft['away']} | {info}")

print("-" * 40)
print(f"Mecze z danymi HT: {with_ht} na {len(matches)} pobranych.")
print(f"Znaleziono łamaków: {lamaki}")

if with_ht > 0:
    print(f"WYNIK PROCENTOWY: {round(lamaki/with_ht*100, 1)}%")
else:
    print("WYNIK: 0% (Brak danych do analizy)")
