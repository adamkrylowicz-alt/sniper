# PATCH: scoring wejść + bezpiecznik dziennej straty (2026-07-23)

Nowy plik `app/services/bot_entry_filters.py` jest **samodzielny** — nic nie
nadpisuje. Poniżej 4 minimalne zmiany w `app/services/bot_engine.py`.

Każda zmiana ma blok ZNAJDŹ (dokładny, istniejący kod) i ZAMIEŃ NA.
Nie zmieniaj niczego poza tymi czterema miejscami.

---

## 1/4 — import nowego modułu

### ZNAJDŹ
```python
from . import bot_credentials, mailer, price_feed
```

### ZAMIEŃ NA
```python
from . import bot_credentials, bot_entry_filters, mailer, price_feed
```

---

## 2/4 — `_process_entries`: scoring zamiast pierwszego-lepszego z bazy

### ZNAJDŹ
```python
    now = dt.datetime.utcnow()
    assets = BotAsset.query.filter_by(user_id=user_id, is_penny_stock=False).all()
    for asset in assets:
        if not _market_open(asset.currency):
            continue  # giełda właściwa dla tej waluty zamknięta - patrz _market_open
        already_open = ActiveTrade.query.filter_by(user_id=user_id, ticker=asset.ticker, status="OPEN").first()
        if already_open:
            continue
        backoff = _entry_fail_backoff.get((user_id, asset.ticker))
        if backoff is not None and now < backoff[1]:
            continue  # asset "w pauzie" po serii nieudanych prób - pomijamy, próbujemy dalej listy
        if _enter_position(user_id, asset, settings):
            return
```

### ZAMIEŃ NA
```python
    now = dt.datetime.utcnow()

    # Krok 1: twarde, DARMOWE odsiewanie (zero zapytań do czegokolwiek) -
    # giełda zamknięta / pozycja już otwarta / asset w backoffie po serii
    # nieudanych prób. Dopiero to co zostanie idzie do scoringu.
    eligible = []
    for asset in BotAsset.query.filter_by(user_id=user_id, is_penny_stock=False).all():
        if not _market_open(asset.currency):
            continue  # giełda właściwa dla tej waluty zamknięta - patrz _market_open
        if ActiveTrade.query.filter_by(user_id=user_id, ticker=asset.ticker, status="OPEN").first():
            continue
        backoff = _entry_fail_backoff.get((user_id, asset.ticker))
        if backoff is not None and now < backoff[1]:
            continue  # asset "w pauzie" po serii nieudanych prób
        eligible.append(asset)

    if not eligible:
        return

    # Krok 2: scoring (patrz services/bot_entry_filters.py). Do 23.07 o tym
    # który asset dostanie slot decydowała KOLEJNOŚĆ WIERSZY W BAZIE (brak
    # order_by) - przy 38 kandydatach i limicie 10 pozycji to była największa
    # strata potencjału w całym silniku. Teraz bot wchodzi w NAJLEPSZEGO.
    #
    # Koszt API: ZERO dodatkowych zapytań. candles_getter korzysta z tego
    # samego 30-minutowego cache co filtr trendu i ATR, a scoring celowo NIE
    # potrzebuje żywej ceny (używa ceny zamknięcia ostatniej świecy) - żywa
    # cena jest pobierana dopiero w _enter_position, dla zwycięzcy.
    scored, stats = bot_entry_filters.rank_candidates(
        eligible,
        settings,
        candles_getter=lambda ticker: price_feed.get_mini_chart_ohlc(
            current_app.config.get("FINNHUB_API_KEY"),
            ticker,
            days=bot_entry_filters.TREND_LOOKBACK_DAYS,
        ),
    )

    if not scored:
        _log(user_id, "INFO", f"Wejścia: żaden kandydat nie przeszedł filtrów ({stats.summary()}).")
        return

    best_ticker = scored[0][0].ticker
    _log(
        user_id, "INFO",
        f"Wejścia: {stats.summary()}. Najlepszy kandydat: {best_ticker} "
        f"(score {scored[0][1]:.3f}).",
    )

    # Krok 3: próbujemy od najlepszego. NAJWYŻEJ JEDNO wejście na tick, ale
    # tylko jeśli faktycznie dotarło do T212 (return dopiero gdy
    # _enter_position zwróci True) - patrz docstring tej funkcji.
    for asset, _score in scored:
        if _enter_position(user_id, asset, settings):
            return
```

---

## 3/4 — `tick()`: egzekwowanie `max_daily_loss`

Do 23.07 `max_daily_loss` było **martwym polem formularza** — żaden kod go nie
czytał (przyznane wprost w docstringu modułu). Bot miał DCA, 10 równoczesnych
pozycji i zero działającego bezpiecznika.

### ZNAJDŹ
```python
            client = _get_client_for_user(user_id, settings)
            if client is not None:
                now = dt.datetime.utcnow()
```

### ZAMIEŃ NA
```python
            # Bezpiecznik dziennej straty - SPRAWDZANY PRZED czymkolwiek innym
            # w tym ticku. Gdy próg przekroczony: bot się wyłącza (is_bot_active
            # =False) i tracimy poświadczenia, więc następne ticki go pominą.
            # Pozycje NIE są zamykane automatycznie - to świadoma decyzja,
            # panic-sell po przekroczeniu progu potrafi zrealizować stratę
            # dokładnie w dołku. Bot przestaje DOKŁADAĆ, resztą zarządzasz ręcznie.
            if not settings.is_paper_trading:
                breach = bot_entry_filters.check_daily_loss_limit(
                    user_id,
                    settings,
                    lambda ticker: price_feed.get_live_price(
                        current_app.config.get("FINNHUB_API_KEY"), ticker,
                        current_app.config.get("ALPACA_API_KEY"),
                        current_app.config.get("ALPACA_API_SECRET"),
                    ),
                )
                if breach is not None:
                    bot_credentials.deactivate(user_id)
                    _log(
                        user_id, "WARN",
                        f"STOP: dzienny limit straty przekroczony. Zrealizowane "
                        f"{breach['realized']:+.2f}, niezrealizowane {breach['unrealized']:+.2f}, "
                        f"razem {breach['total']:+.2f} (limit {settings.max_daily_loss}). "
                        "Bot wyłączony - otwarte pozycje ZOSTAJĄ, zarządź nimi ręcznie.",
                    )
                    continue

            client = _get_client_for_user(user_id, settings)
            if client is not None:
                now = dt.datetime.utcnow()
```

---

## 4/4 — `_enter_position`: usunięcie starego, jednostronnego filtru trendu

Nowy filtr w `bot_entry_filters` jest dwustronny (łapie też wystrzały i piłę)
i działa już na etapie scoringu, więc ten jest teraz martwym duplikatem.

### ZNAJDŹ
```python
    if not _entry_trend_ok(asset.ticker):
        _log(
            user_id, "INFO",
            f"{asset.ticker}: pomijam wejście - cena spadła o więcej niż {ENTRY_TREND_MAX_DROP_PCT * 100}% "
            f"w ostatnich {ENTRY_TREND_LOOKBACK_DAYS} dniach (nie łapiemy spadającego noża).",
        )
        return False

```

### ZAMIEŃ NA
(pusto — usuń te 8 linii)

Funkcja `_entry_trend_ok` i stałe `ENTRY_TREND_*` mogą zostać w pliku
nieużywane — nie usuwaj ich w tym samym kroku, żeby zmiana była łatwa do
cofnięcia. Sprzątanie osobno, jak nowy filtr się sprawdzi.

---

## Po wgraniu

1. `python3 -c "from app import create_app; create_app()"` — sprawdzenie że się importuje
2. Restart appki, obserwuj Dziennik bota — nowy wpis INFO przy każdym ticku
   pokazuje ile kandydatów przeszło filtry i kto wygrał
3. **Zero zmian w bazie** — żadnej nowej kolumny, `$wipeDatabase = $false`

## Czego jeszcze NIE ma (świadomie)

**Filtr spreadu** jest napisany i przetestowany, ale nieaktywny — `rank_candidates`
wołane bez `quote_getter`, więc spread nie jest sprawdzany (fail-open, widoczne
w statystykach jako "bez oceny spreadu"). Żeby go włączyć, potrzebna jest w
`price_feed.py` funkcja zwracająca `{"bid": ..., "ask": ...}` (darmowy tier
Alpaki to daje, klucze już są w configu). Wtedy wystarczy dopisać do wywołania
w punkcie 2/4:

```python
        quote_getter=lambda ticker: price_feed.get_quote_bid_ask(ticker, ...),
```
