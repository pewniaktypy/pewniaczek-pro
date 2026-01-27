import requests
import json
import os

# --- KONFIGURACJA ---
try:
    from config import API_KEY
except ImportError:
    API_KEY = "TUTAJ_WPISZ_KLUCZ_JESLI_NIE_MASZ_CONFIG"

HEADERS = {
    'x-rapidapi-host': "v3.football.api-sports.io",
    'x-rapidapi-key': API_KEY
}

# ID 653 to Cork City w danych historycznych, które widzisz
TEAM_ID = 653 
TEAM_NAME = "Lech Poznan (Cork City ID)"

print(f"--- SPRAWDZANIE DANYCH DLA: {TEAM_NAME} ---")
url = "https://v3.football.api-sports.io/fixtures"
# Pobieramy 50 ostatnich zakończonych meczów
params = {"team": TEAM_ID, "last": 50, "status": "FT"}

try:
    resp = requests.get(url, headers=HEADERS, params=params)
    data = resp.json().get('response', [])
    
    print(f"Pobrano {len(data)} meczów z API.")
    
    # 1. NAJPIERW POKAZUJEMY PRZYKŁADY (Tylko 10)
    print("\n--- PRZYKŁADOWE WYNIKI (PIERWSZE 10) ---")
    for m in data[:10]:
        date = m['fixture']['date'][:10]
        home = m['teams']['home']['name']
        away = m['teams']['away']['name']
        score = m.get('score', {})
        ht = score.get('halftime', {})
        ft = score.get('fulltime', {})
        status_txt = "[OK]" if ht.get('home') is not None else "[BRAK HT!]"
        print(f"{date} | {home} vs {away} | HT: {ht.get('home')}-{ht.get('away')} | FT: {ft.get('home')}-{ft.get('away')} -> {status_txt}")

    # 2. TERAZ LICZYMY STATYSTYKI DLA WSZYSTKICH POBRANYCH
    matches_with_ht = 0
    matches_skipped = 0
    lamaki = 0

    for m in data:
        score = m.get('score', {})
        ht = score.get('halftime', {})
        ft = score.get('fulltime', {})

        # Czy mamy wynik do przerwy?
        if ht.get('home') is not None and ft.get('home') is not None:
            matches_with_ht += 1
            h_ht, a_ht = ht['home'], ht['away']
            h_ft, a_ft = ft['home'], ft['away']
            
            # Sprawdź łamaka
            if (h_ht > a_ht and h_ft < a_ft) or (a_ht > h_ht and h_ft > a_ft):
                lamaki += 1
                print(f"🔥 ZNALEZIONO ŁAMAKA W HISTORII: {m['fixture']['date']} {m['teams']['home']['name']} - {m['teams']['away']['name']}")
        else:
            matches_skipped += 1

    print("\n--- PODSUMOWANIE PRAWIDŁOWE ---")
    print(f"Łącznie sprawdzono: {len(data)}")
    print(f"Mecze z poprawnym wynikiem HT: {matches_with_ht}")
    print(f"Mecze POMINIĘTE (brak danych HT): {matches_skipped}")
    
    valid_matches = matches_with_ht if matches_with_ht > 0 else 1
    percent = (lamaki / valid_matches) * 100
    print(f"Znaleziono łamaków: {lamaki}")
    print(f"Prawdziwy procent: {round(percent, 1)}%")

except Exception as e:
    print(f"Błąd: {e}")