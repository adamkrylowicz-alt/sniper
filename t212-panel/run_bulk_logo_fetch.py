"""
run_bulk_logo_fetch.py
========================
Jednorazowe, SYNCHRONICZNE uruchomienie pobierania logo dla CAŁEJ bazy
instrumentów - niezależne od żywego procesu appki (w odróżnieniu od
przycisku w Ustawieniach, który odpala to samo w tle WEWNĄTRZ procesu
Flask). Ten skrypt uruchamia się bezpośrednio przez SSH/nohup, z paskiem
postępu na stdout co 200 instrumentów, żeby dało się to obserwować zdalnie
bez logowania się do appki przez przeglądarkę.

Bezpieczny do przerwania/wznowienia (fetch_and_cache_logo pomija to co już
jest w cache'u na dysku) i bezpieczny do uruchomienia obok żywej appki
(tylko odczyt z bazy + zapis plików logo, zero zapisów do bazy danych).

Uruchom z katalogu t212-panel:

    nohup python3 -u run_bulk_logo_fetch.py > bulk_logo_fetch.log 2>&1 &
"""
import sys
import time

from app import create_app
from app.models import Instrument
from app.services import logo_cache

app = create_app()

with app.app_context():
    api_key = app.config.get("LOGO_DEV_API_KEY")
    if not api_key:
        print("BRAK LOGO_DEV_API_KEY w .env - przerywam.")
        sys.exit(1)

    tickers = [row.ticker for row in Instrument.query.with_entities(Instrument.ticker)]
    total = len(tickers)
    print(f"Start: {total} instrumentow do sprawdzenia.", flush=True)

    fetched = 0
    start_time = time.monotonic()

    for i, ticker in enumerate(tickers, start=1):
        filename = logo_cache.fetch_and_cache_logo(app.static_folder, ticker, api_key)
        if filename:
            fetched += 1
        if i % 200 == 0 or i == total:
            elapsed = time.monotonic() - start_time
            print(f"{i}/{total} sprawdzonych, {fetched} logo pobranych, {elapsed:.0f}s minelo", flush=True)
        time.sleep(logo_cache.BULK_FETCH_DELAY_SECONDS)

    print(f"GOTOWE: {fetched}/{total} instrumentow ma teraz logo.", flush=True)
