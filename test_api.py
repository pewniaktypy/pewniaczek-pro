import requests
import json
import sqlite3
from datetime import datetime
import pytz

# --- KONFIGURACJA ---
API_KEY = "a9c00564195a59b4641909f9610c378d"  # Twój klucz
HEADERS = {'x-rapidapi-host': "v3.football.api-sports.io", 'x-rapidapi-key': API_KEY}
API_URL = "https://v3.football.api-sports.io/fixtures"
PL_TIMEZONE = pytz.timezone('Europe/Warsaw')

def run_test():
    print("\n" + "="*40)
    print(" 🛠  DIAGNOSTYKA PEWNIACZKA PRO 🛠 ")
    print("="*40)

    # 1. TEST KLUCZA API
    print("\n[1/4] Sprawdzanie statusu konta API...")
    try:
        resp = requests.get("https://v3.football.api-sports.io/status", headers=HEADERS)
        data = resp.json()
        print(f"👉 Status HTTP: {resp.status_code}")
        
        if resp.status_code == 200:
            account = data.get('response', {}).get('account', {})
            reqs = data.get('response', {}).get('requests', {})
            print(f"✅ Konto: {account.get('firstname')} {account.get('lastname')}")
            print(f"✅ Limit dzienny: {reqs.get('limit_day')}")
            print(f"✅ Zużyto dzisiaj: {reqs.get('current')}")
        else:
            print(f"❌ BŁĄD KLUCZA: {data}")
            return
    except Exception as e:
        print(f"❌ BŁĄD POŁĄCZENIA: {e}")
        return

    # 2. TEST POBIERANIA MECZÓW
    today = datetime.now(PL_TIMEZONE).strftime('%Y-%m-%d')
    print(f"\n[2/4] Pobieranie meczów na dzień: {today}...")
    try:
        resp = requests.get(API_URL, headers=HEADERS, params={"date": today})
        matches = resp.json().get('response', [])
        count = len(matches)
        print(f"👉 Znaleziono meczów: {count}")
        
        if count == 0:
            print("⚠️ API zwróciło pustą listę! (Może dzisiaj nie ma meczów w pakiecie?)")
            return
        
        print("✅ Pobieranie działa.")
    except Exception as e:
        print(f"❌ BŁĄD POBIERANIA: {e}")
        return

    # 3. TEST PRZETWARZANIA (Jeden mecz)
    print(f"\n[3/4] Próba analizy pierwszego meczu...")
    sample = matches[0]
    team_home = sample['teams']['home']['name']
    team_away = sample['teams']['away']['name']
    print(f"👉 Mecz: {team_home} vs {team_away}")
    
    # Symulacja błędu Key Error
    print("👉 Sprawdzanie struktury ligi...")
    league = sample.get('league', {})
    l_type = league.get('type')
    print(f"   Typ ligi (raw): {l_type}")
    
    if not l_type:
        print("⚠️ UWAGA: Ten mecz nie ma pola 'type'! (To wywalało stary kod)")
    else:
        print("✅ Struktura OK.")

    # 4. TEST BAZY DANYCH
    print("\n[4/4] Sprawdzanie bazy danych...")
    try:
        conn = sqlite3.connect('pewniaczek.db')
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM matches")
        db_count = cursor.fetchone()[0]
        print(f"👉 Rekordów w bazie: {db_count}")
        conn.close()
        
        if db_count == 0:
            print("❌ Baza jest pusta! Robot nie zapisuje danych.")
        else:
            print("✅ Baza zawiera dane.")
    except Exception as e:
        print(f"❌ Błąd dostępu do bazy: {e}")

    print("\n" + "="*40)
    print(" KONIEC DIAGNOSTYKI ")
    print("="*40 + "\n")

if __name__ == "__main__":
    run_test()
