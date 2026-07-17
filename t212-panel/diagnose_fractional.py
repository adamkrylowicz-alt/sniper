"""
diagnose_fractional.py
========================
Jednorazowy, RĘCZNY skrypt diagnostyczny - sprawdza jakie pola faktycznie
zwraca T212 w /equity/metadata/instruments dla kilku typowych tickerów,
żeby ustalić czy/jak zaimplementować Fractional Guard (sprawdzenie czy dany
instrument wspiera ułamkowe akcje) bez zgadywania z niepewnej dokumentacji.

CELOWO interaktywny (getpass, hasło NIE trafia do żadnego loga/komendy) -
odszyfrowuje Twój klucz API T212 Zero-Knowledge, więc musisz je wpisać sam.

Uruchom RĘCZNIE (nie przez zautomatyzowaną komendę):

    python3 diagnose_fractional.py
"""

import getpass
import sys

from app import create_app
from app.models import ApiKeySet, User
from app.services.t212_client import T212APIError, T212Client
from app import cipher

SAMPLE_TICKERS = ["AAPL_US_EQ", "SPCX_US_EQ", "MSFT_US_EQ", "NVDA_US_EQ"]

app = create_app()

with app.app_context():
    username = input("Username: ").strip()
    password = getpass.getpass("Hasło: ")

    user = User.query.filter_by(username=username).first()
    if user is None:
        print("Nie znaleziono użytkownika.")
        sys.exit(1)

    try:
        master_key = cipher.try_unwrap(user.wrapped_master_key_by_password, password, user.salt_password)
    except cipher.WrongCredentialsError:
        print("Złe hasło.")
        sys.exit(1)

    entry = ApiKeySet.query.filter_by(user_id=user.id, environment="demo").first()
    if entry is None:
        print("Brak zapisanego klucza demo.")
        sys.exit(1)

    import json
    creds = json.loads(cipher.decrypt_secret(entry.encrypted_key, master_key))

    client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment="demo")

    print("Pobieram pełną listę instrumentów (JEDNO zapytanie do T212)...")
    try:
        instruments = client.get_instruments()
    except T212APIError as exc:
        print(f"Błąd T212: {exc}")
        sys.exit(1)

    print(f"Pobrano {len(instruments)} instrumentów. Szukam próbek...")
    by_ticker = {i.get("ticker"): i for i in instruments}

    print("\n=== Pełny JSON dla przykładowych tickerów ===")
    for ticker in SAMPLE_TICKERS:
        item = by_ticker.get(ticker)
        if item is None:
            print(f"\n{ticker}: NIE ZNALEZIONO w liście")
            continue
        print(f"\n--- {ticker} ---")
        print(json.dumps(item, indent=2, ensure_ascii=False))

    print("\n=== Wszystkie unikalne KLUCZE (nazwy pól) występujące w całej liście ===")
    all_keys = set()
    for item in instruments:
        all_keys.update(item.keys())
    print(sorted(all_keys))
