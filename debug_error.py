import requests
import json

# Twój klucz
API_KEY = "a9c00564195a59b4641909f9610c378d"
HEADERS = {'x-rapidapi-host': "v3.football.api-sports.io", 'x-rapidapi-key': API_KEY}

print("\n--- SPRAWDZANIE ODPOWIEDZI API ---")
try:
    # 1. Sprawdź status konta
    resp = requests.get("https://v3.football.api-sports.io/status", headers=HEADERS)
    data = resp.json()
    
    print(f"Status HTTP: {resp.status_code}")
    print("Pełna odpowiedź JSON:")
    print(json.dumps(data, indent=2))
    
    errors = data.get('errors', [])
    if errors:
        print("\n❌ ZNALEZIONO BŁĘDY API:")
        print(errors)
    else:
        print("\n✅ Brak błędów w sekcji 'errors'.")

except Exception as e:
    print(f"Błąd skryptu: {e}")
