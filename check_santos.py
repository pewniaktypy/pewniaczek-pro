import requests
import json
try:
    from config import API_KEY
except:
    API_KEY = "TWOJ_KLUCZ" # Zastąp jeśli trzeba, lub skrypt weźmie z pliku

HEADERS = {'x-rapidapi-host': "v3.football.api-sports.io", 'x-rapidapi-key': API_KEY}
# ID Santos U20 to zazwyczaj 10019 lub podobne, ale znajdziemy po nazwie
print("Szukam Santos U20...")
search = requests.get("https://v3.football.api-sports.io/teams", headers=HEADERS, params={"search": "Santos U20"}).json()['response']
if not search: 
    print("Nie znaleziono Santos U20")
    exit()
    
tid = search[0]['team']['id']
print(f"ID: {tid}. Pobieram 50 meczów (FT)...")

# TO JEST KLUCZOWE ZAPYTANIE:
resp = requests.get("https://v3.football.api-sports.io/fixtures", headers=HEADERS, params={"team": tid, "last": 50, "status": "FT"})
data = resp.json().get('response', [])

print(f"Wynik: Pobrano {len(data)} meczów.")
if len(data) > 0:
    print("SUKCES! Konfiguracja last=50 + status=FT działa.")
else:
    print("PORAŻKA. Nadal pusto.")
