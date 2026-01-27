import requests
import json
import os
import sys

# --- KONFIGURACJA ---
H_ID = 248  # Melbourne Victory
A_ID = 250  # Sydney FC
SEASONS = [2021, 2022, 2023, 2024, 2025, 2026] # Zakres 5 lat

try:
    from config import API_KEY
except ImportError:
    API_KEY = os.environ.get("API_KEY", "BRAK_KLUCZA")

HEADERS = {'x-rapidapi-host': "v3.football.api-sports.io", 'x-rapidapi-key': API_KEY}

def test_5_years():
    print(f"\n--- TEST 5 LAT: Melbourne Victory ({H_ID}) vs Sydney FC ({A_ID}) ---")
    
    total_matches_found = 0
    h2h_matches = []

    # PĘTLA PO LATACH (Zamiast last=50)
    for season in SEASONS:
        print(f"📡 Pobieram sezon {season}...", end="")
        
        url = "https://v3.football.api-sports.io/fixtures"
        # Pobieramy mecze Melbourne z danego sezonu
        params = {"team": H_ID, "season": season, "status": "FT"}
        
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=10)
            data = r.json().get('response', [])
            print(f" OK ({len(data)} meczów)")
            
            # Filtrowanie ręczne: szukamy Sydney FC
            for m in data:
                h = m['teams']['home']['id']
                a = m['teams']['away']['id']
                
                # Jeśli przeciwnikiem jest Sydney (jako dom lub wyjazd)
                if h == A_ID or a == A_ID:
                    h2h_matches.append(m)
                    
        except Exception as e:
            print(f" BŁĄD: {e}")

    # --- WYNIKI ---
    print(f"\n✅ ZNALEZIONO RAZEM: {len(h2h_matches)} meczów bezpośrednich w 5 lat.")
    
    # Sortowanie od najnowszych
    h2h_matches.sort(key=lambda x: x['fixture']['date'], reverse=True)
    
    lamaki = 0
    for m in h2h_matches:
        d = m['fixture']['date'][:10]
        ht = m['score']['halftime']
        ft = m['score']['fulltime']
        
        # Czy łamak?
        is_rev = False
        if ht.get('home') is not None and ft.get('home') is not None:
             if (ht['home'] > ht['away'] and ft['home'] < ft['away']) or \
                (ht['away'] > ht['home'] and ft['home'] > ft['away']):
                 is_rev = True
                 lamaki += 1
        
        rev_txt = " <--- ŁAMAK !!!" if is_rev else ""
        print(f" - {d}: {m['teams']['home']['name']} {m['goals']['home']}-{m['goals']['away']} {m['teams']['away']['name']} (HT: {ht['home']}-{ht['away']}){rev_txt}")

    print(f"\n📊 PODSUMOWANIE STATYSTYK: {lamaki}/{len(h2h_matches)}")

if __name__ == "__main__":
    test_5_years()