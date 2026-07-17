"""
diagnose_logo.py
==================
Jednorazowy skrypt diagnostyczny - sprawdza DOKŁADNIE dlaczego logotypy się
nie pobierają, zamiast zgadywać. Uruchamia się w tym samym środowisku co
appka (te same .env/config.py), więc pokazuje realny stan, nie hipotezy.

Uruchom z katalogu t212-panel:

    python3 diagnose_logo.py [TICKER]

Bez argumentu sprawdza AAPL_US_EQ.
"""

import sys

from app import create_app
from app.services import logo_cache

ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL_US_EQ"

app = create_app()

with app.app_context():
    api_key = app.config.get("LOGO_DEV_API_KEY")

    print("=== 1. Czy klucz jest wczytany z .env? ===")
    if not api_key:
        print("BRAK - LOGO_DEV_API_KEY jest puste/None w config.")
        print("Sprawdz: cat .env | grep LOGO_DEV_API_KEY")
        print("Upewnij sie ze linia wyglada DOKLADNIE tak (bez spacji, bez cudzyslowow):")
        print("  LOGO_DEV_API_KEY=pk_twoj_klucz")
        sys.exit(1)
    else:
        masked = api_key[:6] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
        print(f"OK - klucz wczytany, dlugosc={len(api_key)}, wyglada tak: {masked}")

    print()
    print(f"=== 2. Czy ticker {ticker} mapuje sie na jakis symbol gieldowy? ===")
    symbol = logo_cache._to_market_symbol(ticker)
    if not symbol:
        print(f"BRAK - {ticker} nie mapuje sie na zaden symbol (np. wewnetrzny kod UCITS ETF).")
        print("To NIE jest blad - dla takich tickerow appka celowo pokazuje kolorowy awatar.")
        sys.exit(0)
    print(f"OK - {ticker} -> symbol '{symbol}'")

    print()
    print("=== 3. Prawdziwe zapytanie do Logo.dev (z Twoim kluczem) ===")
    import requests
    url = logo_cache.LOGO_SERVICE_URL.format(symbol=symbol)
    resp = requests.get(url, params={"token": api_key, "fallback": "404"}, timeout=8)
    print(f"URL: {resp.url}")
    print(f"Status: {resp.status_code}")
    print(f"Content-Type: {resp.headers.get('Content-Type')}")
    print(f"Rozmiar odpowiedzi: {len(resp.content)} bajtow")
    if resp.status_code != 200:
        print()
        print(f"Tresc odpowiedzi (blad): {resp.text[:300]}")
        print()
        print("To jest RZECZYWISTY powod braku logo - status inny niz 200")
        print("powoduje ze appka pokazuje awatar zamiast logo (celowe zachowanie).")
    else:
        print()
        print("OK - Logo.dev zwrocilo prawdziwy obrazek. Jesli mimo to w appce nie")
        print("widac logo, sprawdz uprawnienia zapisu do app/static/logos/ (Docker):")
        print("  ls -la app/static/logos/")
        print("  touch app/static/logos/test.txt && rm app/static/logos/test.txt")
