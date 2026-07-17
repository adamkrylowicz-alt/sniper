# Sniper — Trading 212 Panel

## Stack
- Backend: Python (lokalnie 3.11, na NAS-ie faktycznie 3.8), Flask (application factory pattern)
- Frontend: HTML/JS/CSS (Jinja2 templates)
- Baza: SQLite (instance/sniper.db)
- Deploy: Synology NAS, **goły proces `python3 run.py`** (NIE Docker — `docker` nie ma nawet na PATH w SSH), port 5050, dostęp w LAN przez http://192.168.1.50:5050
- Upload: `t212-panel/upload_sniper.ps1` (SCP/PowerShell) — UWAGA: jest też stary, nieaktualny `pliki/upload_sniper.ps1` z innej maszyny, nie używać

## Struktura
- `t212-panel/run.py` — punkt wejścia (`app.run(debug=True)`)
- `t212-panel/app/__init__.py` — fabryka aplikacji (`create_app()`), rejestracja blueprintów, scheduler, root `/` redirect
- `t212-panel/app/routes/*.py` — blueprinty: auth, scalping (Warp+Focus), settings, api_keys, report, pie, bot
- `t212-panel/app/services/*.py` — logika: t212_client, instrument_cache, logo_cache, risk_guard, session_store, bot_engine, price_feed, finnhub_client, mailer
- `t212-panel/app/templates/`, `t212-panel/app/static/` — HTML/JS/CSS
- `t212-panel/migrate_add_*.py` — jednorazowe, idempotentne migracje SQLite (ALTER TABLE) — uruchamiane ręcznie po uploadzie, bo `db.create_all()` nie dokłada kolumn do istniejących tabel

## Ważne konteksty
- T212 API: rate limity na demo są bardzo restrykcyjne, niektóre endpointy zwracają 403 na demo
- Zero-Knowledge encryption na auth (cipher.py) — master_key nigdy nie trafia do bazy w formie jawnej
- Warp Mode = one-click trading grid (siatka 3x3)
- Focus Mode = karuzela dużych kafelków z ceną live/sparkline z Finnhub (dodane 17.07.2026)
- Smart Virtual Pie = "wirtualne ETF-y" (koszyki instrumentów z proporcjonalnymi wagami)
- Micro-Grid Bot = automatyczny bot działający na APScheduler (tick co 60s), patrz `extensions.py::scheduler`
- Ceny/wykresy: Finnhub (klucz `FINNHUB_API_KEY`) jako główne źródło, Yahoo Finance jako fallback dla bota
- Logotypy spółek: Logo.dev (`LOGO_DEV_API_KEY`) — Clearbit jest MARTWE od grudnia 2025, nie wracać do niego

## Styl kodu
- Komentarze po polsku
- All-in-one fixes — nie rozbijaj na osobne pliki bez potrzeby

## Dziennik zmian
- **2026-07-17**: Przypadkowe nadpisanie części plików przez sesję Claude web (pracowała na starszej bazie kodu, bez kontekstu wczorajszych poprawek) spowodowało utratę: scheduler bota, migracji Logo.dev, per-user RiskGuard, powiązania `OrderLog.pie_id`. Odtworzone przez scalenie dobrej lokalnej wersji z nowo dodanym Focus Mode (nic nie stracone). Przy okazji naprawione dwa błędy: brak widoku dla `/` (404) i `login_required` ustawiający `next=` nawet dla żądań POST (dawało 405 po zalogowaniu). Repo git zainicjalizowane tego dnia.
- **2026-07-17 (runda 2 - UI/wykresy)**: Dodane logo/ikonki aktywów w Warp Mode (infrastruktura logo_cache już istniała, tylko nie była podpięta do warp.html/scalping.py). Naprawiony CSS: Focus Mode >3 kafelki teraz zawijają się do kolejnego wiersza (`flex-wrap`) zamiast znikać, navbar wyśrodkowany (`.topbar` na CSS grid zamiast `space-between`). Odkryto że Finnhub `/stock/candle` jest zablokowany na obecnym (darmowym) kluczu - dodano fallback na Yahoo Finance Chart API (bez klucza) w `finnhub_client.py::get_sparkline` i `price_feed.py::get_mini_chart`. Dodany wykres kołowy (donut, czysty CSS `conic-gradient`, bez biblioteki) alokacji koszyka w Smart Virtual Pie - kolory segmentów zgodne z kolorami awatarów aktywów.
- **2026-07-17 (runda 3 - bot)**: Znaleziony i naprawiony realny bug bezpieczeństwa: `bot_engine.py::_enter_position()` NIGDY nie sprawdzał `RiskSettings.is_paper_trading` - checkbox "Paper Trading" w UI nic nie robił, bot zawsze wysyłał prawdziwe zlecenia do T212 demo (potwierdzone: pozycja SPCX_US_EQ z 16.07 wieczorem powstała mimo zaznaczonego paper trading). Dodana kolumna `ActiveTrade.is_paper` (migracja `migrate_add_active_trade_is_paper.py`) - gdy paper trading włączone, bot loguje symulowane wejście z syntetycznymi ID zleceń, zero requestów do T212; `reconcile()` pomija pozycje papierowe. Poprawione mylące opisy w bot.html UI.
