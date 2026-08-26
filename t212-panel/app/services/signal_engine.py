"""
app/services/signal_engine.py
================================
Strategia sygnałowa RSI/MA/ATR ("Normalny tryb handlu" z docs/IDEAS_v2.md) -
OSOBNA od Micro-Grid Bota (bot_engine.py), decyzja Adama 2026-07-24: działa
RÓWNOLEGLE, nie zastępuje siatki DCA. Własne tabele (models.py::SignalAsset/
SignalTrade/SignalSettings/SignalAuditLog), własny scheduler job (60s, patrz
app/__init__.py), własna strona (/signal/, routes/signal.py).

Warunek wejścia (PRD, na sztywno - NIE builder AND/OR, świadomie odłożony,
patrz docs/IDEAS_v2.md "Otwarte pytania"): RSI(14) < próg (domyślnie 35) ORAZ
cena > SMA(200), liczone ze świec DZIENNYCH (w odróżnieniu od modułu EOD,
który ma działać na świecach 1-min - ten moduł jeszcze nie zaimplementowany).
JEDNO wejście na sygnał, bez DCA.

Zarządzanie ryzykiem: Stop Loss = ATR(14) * stop_loss_atr_mult (domyślnie
1.8), liczony PRZY WEJŚCIU jako punkt startowy, ale od tej chwili TRAILING
(patrz niżej) - PEŁNY, bez sztywnego sufitu.

HISTORIA: do 24.07 SL i "Take Profit" (ATR * take_profit_atr_mult, domyślnie
3.0) byly OBA stałe przez cały czas trwania pozycji (świadome uproszczenie,
"sprawdzimy w boju" zanim dokładać complexity trailing). 28.07 dodany
trailing SL (patrz niżej). TEGO SAMEGO dnia Adam zauważył realny problem ze
sztywnym TP: "jak wyjebie świece w górę to może się zrealizować i już nie
wróci do pozycji" - świeca, która przebije TP, sprzedaje CAŁĄ pozycję od
razu, tracąc dalszy ruch w górę, bo strategia wymaga NOWEGO sygnału wejścia
(RSI<próg + cena>SMA200) żeby wrócić - po takim skoku mało prawdopodobne w
najbliższym czasie. Decyzja: "usuń sztywny take-profit, zrób pełny
trailing" - sztywna sprzedaż na TP CAŁKOWICIE usunięta z `_manage_exits()`/
`_manage_paper_exits()`, jedyne wyjście to teraz trailing stop-loss LUB
ręczne zamknięcie. `take_profit_price` w `SignalTrade` ZOSTAJE (kolumna
NOT NULL, dalej liczona przy wejściu i logowana) jako WYŁĄCZNIE orientacyjny
punkt odniesienia z chwili wejścia - już NIE wyzwala żadnej sprzedaży, patrz
`price_feed`/`routes/scalping.py::/warp/trade_levels`, który już go nie
zwraca dla Sygnału (nieaktualna linia na wykresie tylko by myliła).

TRAILING STOP-LOSS (dodane 2026-07-28, patrz
[[feedback_snajper_profit_protection_priority]] w pamięci Claude - "ochrona
zysku" to stała zasada dla WSZYSTKICH botów, ta sama co Micro-Grid) -
`_trail_stop_loss()`, wołane z `_manage_exits()` na KAŻDYM ticku dla każdej
otwartej pozycji (nie tylko dopóki cena < TP - TP już nic nie robi, patrz
wyżej), przesuwa stop-loss W GÓRĘ (nigdy w dół) o tę SAMĄ odległość co przy
wejściu (`atr_at_entry * stop_loss_atr_mult`, licząc od BIEŻĄCEJ ceny
zamiast ceny wejścia) - naturalnie zaczyna działać dopiero gdy pozycja jest
na plusie (bo dopiero wtedy `cena - dystans > stop przy wejściu`), więc
chroni WYŁĄCZNIE już zarobiony zysk. Prostszy model niż ciągły trailing
Micro-Grid (`_manage_trailing_exit` - tam dwie fazy: szeroki floor przy
pierwszym uzbrojeniu, potem ciasny `current_price*(1-step)`) - tu jeden,
spójny dystans przez cały czas trwania pozycji.

Mechanika wyjścia - TA SAMA przyczyna co przeprojektowanie Micro-Grid
21.07.2026 (T212 nie pozwala trzymać LIMIT SELL + STOP jednocześnie na te
same udziały, patrz docs/IDEAS_v2.md pkt 4): stop-loss to PRAWDZIWY,
PRZESUWANY resting STOP na T212 (chroni nawet offline) - jedyne resting
zlecenie na pozycję, jedyny mechanizm wyjścia po usunięciu sztywnego TP.

Poświadczenia: reużywa services/bot_credentials.py (WSPÓLNY magazyn
odszyfrowanego master_key w pamięci procesu) zamiast własnego mechanizmu -
to ten sam demo klucz T212 co Micro-Grid, więc Adam nie powinien podawać
hasła dwa razy. Włączenie JEDNEGO z dwóch botów odblokowuje poświadczenia
dla OBU; wyłączenie danego bota czyści poświadczenia TYLKO gdy drugi też
jest wyłączony (patrz routes/signal.py::deactivate / routes/bot.py::deactivate).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal, InvalidOperation

import pytz
from flask import current_app

from ..extensions import db
from ..models import SignalAsset, SignalAuditLog, SignalSettings, SignalTrade
from ..routes.api_keys import get_decrypted_credentials
from ..routes.scalping import _log_order
from ..utils import current_environment, humanize_ticker_prefix, should_notify_environment, telegram_env_tag, ticker_display_name
from . import bot_credentials, diagnostics, market_hours, position_alerts, price_feed, price_watchdog, sector_diversity, telegram_notify
from .market_data_keys import get_decrypted_market_data_keys
from .bot_engine import _FILLED_ORDER_STATUS, _extract_owned_quantity_from_error, _lookup_recent_order, _next_retry_delay, _place_buy_with_precision_fallback
from .strategy import signal_strategy
from .strategy.microgrid_strategy import compute_equity_scaled_amount
from .t212_client import T212APIError, T212Client

_AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")

# SIGNAL_ENVIRONMENT jako stała USUNIĘTA 2026-08-06 - patrz identyczny
# komentarz w bot_engine.py przy `utils.current_environment`. Stara teza
# "T212 nie wspiera LIMIT/STOP na live" OBALONA empirycznie 2026-08-06/07 -
# LIMIT i STOP-LIMIT ręcznie potwierdzone działające na live.

RSI_PERIOD = 14
MA_PERIOD = 200
ATR_PERIOD = 14
# Bufor kalendarzowy dla MA(200): ~200 sesji giełdowych ≈ 280-290 dni
# kalendarzowych (weekendy/święta) - 250 zamawianych z Yahoo (patrz
# price_feed._yahoo_range_for_days) zostawia margines dla RSI(14) na ogonie.
SIGNAL_LOOKBACK_DAYS = 250

# DODANE 2026-08-03 (Adam: realny budżet ~1000€, "ogranicz do 1-2 otwartych
# pozycji na bota, nie pięć/dziesięć") - do tej pory `_process_entries` NIE
# MIAŁO ŻADNEGO limitu (w odróżnieniu od bot_engine.py::MAX_CONCURRENT_
# POSITIONS) - iterowało WSZYSTKIE 58 tickerów z listy i otwierało pozycję
# na KAŻDYM który akurat spełnił RSI<próg+cena>SMA200, bez ograniczenia
# liczby ani nawet jednego-wejścia-na-tick jak ma Micro-Grid. Przy szerokim
# spadku rynku (wiele tickerów jednocześnie "oversold") mogło to teoretycznie
# otworzyć dziesiątki pozycji naraz - przy małym budżecie realne ryzyko
# przekroczenia dostępnego kapitału.
#
# PRZENIESIONE 2026-08-03 (wieczorem) ze stałej modułowej do
# SignalSettings.max_concurrent_positions (edytowalne w UI per-user, bez
# redeployu) - Adam: "to tylko ustawienia fabryczne", 2 zostaje jako DEFAULT
# nowej kolumny (patrz migrate_add_max_concurrent_positions.py).

# Godziny wejścia - USUNIĘTE 2026-08-05 (Adam: "boty na usa dzialaja 24/5
# eu dziala 5dni od 9 do 17.30") - Sygnał miał WŁASNE, węższe okno
# (10:00-15:45 EUR / 15:35-21:45 USD) NIEZALEŻNE od wspólnego
# market_hours.is_market_open(). Teraz Sygnał ufa WYŁĄCZNIE wspólnej funkcji
# (EU_SESSION_WINDOW=9:00-17:30, USD=24/5 bez okna) - jedno źródło prawdy
# o godzinach wejść dla wszystkich 3 silników zamiast trzech niezależnych
# definicji które mogły (i tu faktycznie zaczęły) się rozjeżdżać.

# Cooldown ponownego wejścia po stop-lossie (dodane 2026-08-05, na żywo
# znaleziony bug: IFXd_EQ 28.07 między 11:41 a 11:56 dostało 10 wejść z rzędu,
# każde natychmiast stop-lossowane na praktycznie tej samej cenie (~-6%),
# strata ~-66€ w 15 minut - RSI<próg pozostawało prawdziwe tick po ticku bo
# cena leciała w dół, więc _process_entries wchodziło ponownie zaraz po
# zamknięciu poprzedniej pozycji. 29.07 ten sam ticker (już większą pozycją,
# 18.79 szt.) powtórzył scenariusz, -60€ w jednym strzale - razem te dwa
# epizody to ~126€ z ~139€ całej straty Sygnału w tym okresie.
# Backtestowane (backtest/signal_runner.py, parametry produkcyjne RSI<35/
# SL=3xATR/TP=3xATR, 58 tickerów, 400 dni): cooldown 0-10 dni handlowych
# praktycznie NIE zmienia win rate ani total P&L strategii (195.09€ przy
# cooldown=0 vs 193.83€ przy cooldown=3d na 130 transakcjach) - oczekiwane,
# bo dobowy backtest z natury MA już 1 transakcję/dzień/ticker, więc nigdy
# nie zobaczy klastra intraday. Innymi słowy: cooldown nic nie kosztuje
# statystycznie, a usuwa CAŁĄ klasę bugu. 72h (3 dni) pokrywa z zapasem oba
# żywe incydenty (15 min i ~26h odstępu).
STOP_LOSS_REENTRY_COOLDOWN = dt.timedelta(hours=72)

# Backoff dla tick()::get_pending_orders po błędzie T212API (dodane 2026-07-28:
# znalezione na żywo - Micro-Grid już się wycofywał po serii 429, ale Sygnał i
# EOD dalej dobijały się o get_pending_orders CO 60s BEZ PRZERWY, bo żaden z
# nich nie miał własnego backoffu - non-stop bombardowanie WSPÓLNEGO dla
# wszystkich trzech silników, ciasnego limitu T212 demo nie dawało kontu
# żadnej szansy się zresetować, 429 ciągnęło się 12+h zamiast typowych paru
# minut) PRZENIESIONY do t212_client.py::get_pending_orders_for_tick()
# (2026-07-29 - był tu WŁASNY licznik/zegar, niezależny od tego samego w
# bot_engine.py/eod_engine.py, mimo że wszystkie trzy dobijają się o TEN SAM
# limit - Adam: "boty nie widza o sobie i napierdlaja w ten sam czas", złapane
# na żywo: Sygnał złapał 5 kolejnych 429 mimo że Micro-Grid/EOD w tym samym
# czasie ticowały bez błędu). _manage_exits (trailing stop-loss, ochrona
# zysku) CELOWO nie jest tu blokowany, patrz tick() niżej i
# [[feedback_snajper_profit_protection_priority]] - backoff dotyczy
# WYŁĄCZNIE potwierdzania nowych wejść/detekcji wykonania stopa.


# Backoff (minuty) dla DRUGIEGO calla w _confirm_pending_entries -
# get_position() per pozycja, wołane ZARAZ PO udanym get_pending_orders()
# (patrz tick() wyżej) - na bardzo ciasnym limicie demo (x-ratelimit-limit
# widziane jako 0/1 pozostało) dwa udane calle T212 pod rząd w tym samym
# ticku to loteria, więc bez backoffu ten drugi call próbowałby ZNOWU na
# każdym kolejnym udanym ticku, mimo że przed chwilą zawiódł - dokładając
# kolejne zapytanie do tego samego, już wyczerpanego budżetu. Ten sam
# wzorzec co _entry_fail_backoff w bot_engine.py (w pamięci procesu, per
# (user_id, ticker), NIE per-trade w bazie - prostsze niż kolumny
# sell_retry_count/next_sell_retry_at w ActiveTrade, bo SignalTrade nie ma
# ich odpowiednika i nie ma potrzeby przeżywać restartu appki).
CONFIRM_FAIL_BACKOFF_MINUTES = (1, 2, 5, 15, 30)
_confirm_fail_backoff: dict[tuple[int, str], tuple[int, dt.datetime]] = {}


def _next_confirm_fail_delay(consecutive_fails: int) -> dt.timedelta:
    idx = min(consecutive_fails - 1, len(CONFIRM_FAIL_BACKOFF_MINUTES) - 1)
    return dt.timedelta(minutes=CONFIRM_FAIL_BACKOFF_MINUTES[idx])


def _log_block_reason_throttled(user_id: int, key: str, message: str) -> None:
    """Dedupe (30 min/klucz) dla raportu /why (telegram_commands.py,
    2026-08-09) - patrz diagnostics.should_log_throttled, scalone tam z 3
    identycznych kopii. Prefiks "signal:" żeby nie kolidować z tymi samymi
    kluczami w bot_engine.py/eod_engine.py na dzielonym słowniku."""
    if diagnostics.should_log_throttled(user_id, f"signal:{key}"):
        _log(user_id, "INFO", message)


def _log(user_id: int, action_type: str, message: str) -> None:
    # Dodane 2026-07-30: kopia KAŻDEGO wpisu do ukrytego logu diagnostycznego
    # (diagnostics.py) - nie zmienia nic z poniższego (SignalAuditLog/Dziennik
    # zostają jak były, ERROR nadal też leci do current_app.logger.error).
    diagnostics.log_diag(user_id, "signal", f"[{action_type}] {message}")
    if action_type == "ERROR":
        current_app.logger.error("[signal user=%s] %s", user_id, message)
        if should_notify_environment(user_id):
            telegram_notify.send_telegram_message(
                current_app.config.get("TELEGRAM_BOT_TOKEN"), current_app.config.get("TELEGRAM_CHAT_ID"),
                f"🔴 [{telegram_env_tag(user_id)}] Sygnał ERROR (user {user_id}): {humanize_ticker_prefix(message)}",
            )
    entry = SignalAuditLog(
        user_id=user_id, action_type=action_type, message=humanize_ticker_prefix(message),
        environment=current_environment(user_id),
    )
    db.session.add(entry)
    db.session.commit()


def _compute_rsi(closes: list[Decimal], period: int = RSI_PERIOD) -> Decimal | None:
    """
    RSI PROSTY (nie wygładzanie Wildera) - ta sama filozofia prostoty co
    bot_engine.py::_compute_atr ("dla prostoty"). Wymaga co najmniej
    period+1 zamknięć (potrzeba `period` różnic dzień-do-dnia).
    """
    if len(closes) < period + 1:
        return None
    window = closes[-(period + 1):]
    deltas = [window[i] - window[i - 1] for i in range(1, len(window))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = (sum(gains) / period) if gains else Decimal("0")
    avg_loss = (sum(losses) / period) if losses else Decimal("0")
    if avg_loss == 0:
        return Decimal("100") if avg_gain > 0 else Decimal("50")
    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (1 + rs))


def _compute_sma(closes: list[Decimal], period: int = MA_PERIOD) -> Decimal | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    return sum(window) / period


def _compute_atr(candles: list[dict] | None, period: int = ATR_PERIOD) -> Decimal | None:
    """
    Identyczna formuła co bot_engine.py::_compute_atr - kopia celowa (patrz docstring modułu, "osobna strategia").

    PERF (2026-08-02, znalezione przy próbie grid searchu na 58 tickerach -
    58 tickerów x 36 konfiguracji parametrów zabite po 23 min bez postępu):
    stara wersja liczyła True Range dla CAŁEGO przekazanego okna (u wołających
    w signal_runner.py/bot_engine.py to ~205 świec), a dopiero na końcu brała
    ostatnie `period` (14) wartości - 14x niepotrzebnej pracy na wywołanie,
    wołane co dzień dla każdej otwartej pozycji. Przycięcie do `period+1`
    PRZED pętlą daje IDENTYCZNY wynik (TR[i] zależy tylko od świec i/i-1,
    ostatnie `period` TR nie zależą od tego ile świec jest przed nimi) -
    zweryfikowane 200 losowymi testami + przypadkami brzegowymi przed zmianą.
    """
    if not candles or len(candles) < period + 1:
        return None
    window = candles[-(period + 1):]
    true_ranges = []
    for i in range(1, len(window)):
        high = Decimal(str(window[i]["h"]))
        low = Decimal(str(window[i]["l"]))
        prev_close = Decimal(str(window[i - 1]["c"]))
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(true_ranges) / len(true_ranges)


def _finalize_closed_trade(user_id: int, trade: SignalTrade, via: str, fill_price: Decimal) -> None:
    trade.close_price = fill_price
    trade.status = "CLOSED"
    trade.closed_via = via
    trade.closed_at = dt.datetime.utcnow()
    db.session.commit()
    pnl = (fill_price - trade.buy_price) * trade.quantity
    _log(
        user_id, "INFO",
        f"{ticker_display_name(trade.ticker)}: pozycja zamknięta ({via}) @ ~{fill_price}, "
        f"P/L ~{pnl:.2f} {trade.currency}.",
    )


def close_trade_manual(user_id: int, trade: SignalTrade) -> tuple[bool, str]:
    """
    Ręczne zamknięcie pozycji - wydzielone z routes/signal.py::close_position
    (Adam, 2026-08-09: dodanie `/close` na Telegramie) żeby route webowa i
    komenda Telegram dzieliły JEDNĄ implementację zamiast dwóch kopii tego
    samego cancel-stop -> market-sell -> finalize. Różnica względem
    oryginalnej route: klucz API przez `bot_credentials.get_master_key`
    zamiast `current_master_key()` (Telegram nie ma sesji/`g` - ten sam
    wzorzec co `bot_engine.py::adopt_confirmed_signal`). Zwraca (ok, msg)
    zamiast `jsonify` - wołający (route czy Telegram) decyduje o prezentacji.
    """
    market_keys = get_decrypted_market_data_keys(user_id, bot_credentials.get_master_key(user_id))
    price = price_feed.get_live_price(
        market_keys.get("finnhub_api_key"), trade.ticker,
        market_keys.get("alpaca_api_key"), market_keys.get("alpaca_api_secret"),
        market_keys.get("ibkr_host"), market_keys.get("ibkr_port"),
    )
    name = ticker_display_name(trade.ticker)

    if trade.is_paper:
        _finalize_closed_trade(user_id, trade, "manual", fill_price=price or trade.buy_price)
        return True, f"{name}: zamknięte (paper) @ ~{price or trade.buy_price}."

    if not trade.buy_confirmed:
        return False, f"{name}: zlecenie kupna jeszcze nie potwierdzone - poczekaj aż się wykona."

    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        return False, "Sygnał nie ma aktywnych poświadczeń (aktywuj go w appce)."
    env = current_environment(user_id)
    creds = get_decrypted_credentials(user_id, master_key, env)
    if creds is None:
        return False, "Brak zapisanego klucza API."
    client = T212Client(api_key=creds["api_key"], api_secret=creds["api_secret"], environment=env, engine="signal", user_id=user_id)

    if trade.stop_order_id:
        try:
            client.cancel_order(trade.stop_order_id)
        except T212APIError as exc:
            return False, f"{name}: nie udało się anulować stop-loss przed ręczną sprzedażą - {exc}"

    try:
        sell_result = client.place_market_order(trade.ticker, -trade.quantity)
    except T212APIError as exc:
        return False, f"{name}: sprzedaż Market nie powiodła się - {exc}. STOP już zdjęty, pozycja NIECHRONIONA."

    _log_order(
        user_id=user_id, ticker=trade.ticker, side="sell", quantity=trade.quantity,
        price_snapshot=price, status="sent", t212_order_id=sell_result.order_id,
    )
    _finalize_closed_trade(user_id, trade, "manual", fill_price=price or trade.buy_price)
    return True, f"{name}: zamknięte @ ~{price or trade.buy_price}."


def _get_current_equity(
    user_id: int, settings: SignalSettings, client: T212Client | None = None,
) -> Decimal | None:
    """
    Equity CAŁEGO konta T212 - ten sam wzorzec co bot_engine.py::_get_current_equity
    (money management √equity, patrz SignalSettings.equity_sizing_enabled),
    zduplikowany zamiast reużyty przez import - bot_engine.py-owa wersja
    hardkoduje tag "bot" w diagnostics.log_diag, więc reużycie zaciemniłoby
    log diagnostyczny (patrz [[project_snajper_diagnostics_log]]).
    """
    if client is None:
        master_key = bot_credentials.get_master_key(user_id)
        if master_key is None:
            return None
        creds = get_decrypted_credentials(user_id, master_key, current_environment(user_id))
        if creds is None:
            return None
        client = T212Client(
            api_key=creds["api_key"], api_secret=creds["api_secret"], environment=current_environment(user_id),
            engine="signal", user_id=user_id,
        )
    try:
        raw = client.get_cash()
        return Decimal(str(raw["total"]))
    except (T212APIError, InvalidOperation, TypeError, KeyError) as exc:
        diagnostics.log_diag(
            user_id, "signal", f"equity sizing: nie udało się pobrać equity ({exc}) - baza bez skalowania.",
        )
        return None


def _enter_position(
    user_id: int, client: T212Client | None, asset: SignalAsset, settings: SignalSettings,
    price: Decimal, atr: Decimal, current_equity: Decimal | None = None,
) -> bool:
    """
    Zwraca True gdy pozycja faktycznie została otwarta (paper LUB realne
    zlecenie złożone), False gdy odrzucona (walidacja/błąd T212) - ZMIANA
    2026-08-05 (patrz _process_entries niżej, ranking kandydatów zamiast
    "pierwszy pasujący") - wywołujący próbuje KOLEJNEGO w rankingu gdy
    zwycięzca akurat zawiedzie, ten sam wzorzec co bot_engine.py::
    _process_entries.
    """
    effective_entry_amount = asset.entry_amount
    if settings.equity_sizing_enabled and current_equity is not None:
        effective_entry_amount = compute_equity_scaled_amount(
            asset.entry_amount, current_equity, settings.equity_sizing_baseline or Decimal("0"),
        )

    # Koszt przewalutowania (FX) dla tickerów USD na koncie EUR (patrz
    # SignalSettings.fx_cost_adjustment_enabled) - PODBIJA próg stop-loss/
    # take-profit, NIE dotyka ceny użytej do wyliczenia quantity.
    fx_reference_price = None
    if asset.currency == "USD" and settings.fx_cost_adjustment_enabled:
        fx_reference_price = price * (1 + settings.fx_fee_pct * 2)

    try:
        decision = signal_strategy.compute_entry(
            effective_entry_amount, price, atr, settings.stop_loss_atr_mult, settings.take_profit_atr_mult,
            fx_reference_price=fx_reference_price,
        )
    except signal_strategy.EntryValidationError as exc:
        _log(user_id, "ERROR", f"{ticker_display_name(asset.ticker)}: {exc}")
        return False
    quantity = decision.quantity
    stop_loss_price = decision.stop_loss_price
    take_profit_price = decision.take_profit_price

    if settings.is_paper_trading:
        # Symulacja - zero requestow do T212, ten sam wzorzec co bot_engine.py::_enter_position.
        trade = SignalTrade(
            user_id=user_id, signal_asset_id=asset.id, ticker=asset.ticker, currency=asset.currency,
            buy_order_id=f"PAPER-{uuid.uuid4()}",
            buy_price=price, quantity=quantity, allocated_value=quantity * price,
            atr_at_entry=atr, stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
            status="OPEN", is_paper=True, buy_confirmed=True,
            environment=current_environment(user_id),
        )
        db.session.add(trade)
        db.session.commit()
        _log(
            user_id, "BUY",
            f"[PAPER] {ticker_display_name(asset.ticker)}: sygnał wejścia, {quantity} @ ~{price} - "
            f"SL {stop_loss_price:.4f} (trailing) / TP orientacyjny {take_profit_price:.4f} (ATR={atr:.4f}).",
        )
        return True

    try:
        existing_position = client.get_position(asset.ticker)
    except T212APIError:
        existing_position = None
    baseline = Decimal(str(existing_position["quantity"])) if existing_position else Decimal("0")

    try:
        buy_result, quantity = _place_buy_with_precision_fallback(client, asset.ticker, quantity, price)
    except T212APIError as exc:
        _log(user_id, "ERROR", f"{ticker_display_name(asset.ticker)}: błąd składania zlecenia kupna - {exc}")
        return False

    trade = SignalTrade(
        user_id=user_id, signal_asset_id=asset.id, ticker=asset.ticker, currency=asset.currency,
        buy_order_id=buy_result.order_id, baseline_owned_quantity=baseline,
        buy_price=price, quantity=quantity, allocated_value=quantity * price,
        atr_at_entry=atr, stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
        status="OPEN", is_paper=False, buy_confirmed=False,
        environment=current_environment(user_id),
    )
    db.session.add(trade)
    db.session.commit()

    _log_order(
        user_id=user_id, ticker=asset.ticker, side="buy", quantity=quantity,
        price_snapshot=price, status="sent", t212_order_id=buy_result.order_id,
    )
    _log(
        user_id, "BUY",
        f"{ticker_display_name(asset.ticker)}: sygnał wejścia (RSI<{settings.rsi_threshold}, cena>MA{MA_PERIOD}), "
        f"{quantity} @ ~{price} - SL {stop_loss_price:.4f} (trailing) / TP orientacyjny {take_profit_price:.4f} (ATR={atr:.4f}). "
        "Czeka na potwierdzenie kupna, dopiero potem uzbroi stop-loss.",
    )
    return True


def _stop_loss_cooldown_until(user_id: int, ticker: str) -> dt.datetime | None:
    """None gdy wejście dozwolone, inaczej moment (UTC) do kiedy trwa cooldown po ostatnim stop-lossie."""
    last = (
        SignalTrade.query
        .filter_by(
            user_id=user_id, ticker=ticker, status="CLOSED", closed_via="stop-loss",
            environment=current_environment(user_id),
        )
        .order_by(SignalTrade.closed_at.desc())
        .first()
    )
    if last is None or last.closed_at is None:
        return None
    cooldown_until = last.closed_at + STOP_LOSS_REENTRY_COOLDOWN
    return cooldown_until if dt.datetime.utcnow() < cooldown_until else None


def _process_entries(
    user_id: int, client: T212Client | None, settings: SignalSettings, current_equity: Decimal | None = None,
) -> None:
    env = current_environment(user_id)
    assets = SignalAsset.query.filter_by(user_id=user_id, environment=env).all()
    if not assets:
        return

    open_tickers = {
        t.ticker for t in
        SignalTrade.query.filter_by(user_id=user_id, status="OPEN", environment=env).all()
    }
    if len(open_tickers) >= settings.max_concurrent_positions:
        _log_block_reason_throttled(
            user_id, "max_concurrent",
            f"Wejścia: limit pozycji osiągnięty ({len(open_tickers)}/{settings.max_concurrent_positions}) - nic nowego dziś.",
        )
        return  # limit otwartych pozycji osiągnięty (patrz SignalSettings.max_concurrent_positions) - nic nowego dziś

    market_keys = get_decrypted_market_data_keys(user_id, bot_credentials.get_master_key(user_id))
    api_key = market_keys.get("finnhub_api_key")
    alpaca_key = market_keys.get("alpaca_api_key")
    alpaca_secret = market_keys.get("alpaca_api_secret")
    ibkr_host = market_keys.get("ibkr_host")
    ibkr_port = market_keys.get("ibkr_port")

    # ZMIANA 2026-08-05 (Adam: "czy spr tez inne i wybiera najlepsze czy wali
    # po kolei i spr czy sie lapia?" -> "napraw to") - do tej pory ta pętla
    # wchodziła w PIERWSZEGO kandydata z kolejności wierszy w bazie, który
    # akurat spełniał RSI<próg+trend, bez porównania z resztą (w
    # odróżnieniu od bot_engine.py, który od 23.07 ocenia WSZYSTKICH i
    # wchodzi w najlepszego - patrz bot_entry_filters.py). Teraz: dwie fazy,
    # ten sam wzorzec co Micro-Grid. Faza 1 (ta pętla) zbiera WSZYSTKICH
    # kwalifikujących się kandydatów (bez dotykania T212 - tylko Finnhub/
    # Yahoo/Alpaca, ten sam koszt co dawniej, cache 30 min). Faza 2 (niżej)
    # sortuje po sile sygnału i próbuje wejść od najlepszego, z fallbackiem
    # na kolejnego gdyby zwycięzca akurat zawiódł (429/odrzucone zlecenie) -
    # NAJWYŻEJ JEDNO faktyczne wejście na tick, ten sam powód rate-limitowy
    # co zawsze (patrz docstring MAX_CONCURRENT_POSITIONS wyżej).
    #
    # Score = (próg RSI - RSI) - im bardziej wyprzedany kandydat WEWNĄTRZ
    # już potwierdzonego trendu (cena>SMA200, warunek wejścia bez zmian),
    # tym silniejszy sygnał mean-reversion wg własnej tezy Sygnału. Świadomie
    # NIE reużyto scoringu bot_entry_filters.py (kalibrowany pod "spokojny
    # trend, nisko w zakresie dnia" - filozofia DCA Micro-Gridu) - Sygnał ma
    # inną tezę (RSI-momentum w potwierdzonym uptrendzie), więc potrzebuje
    # własnego kryterium, nie cudzego.
    candidates: list[tuple] = []  # (asset, score, price, atr)

    for asset in assets:
        if asset.ticker in open_tickers:
            continue
        cooldown_until = _stop_loss_cooldown_until(user_id, asset.ticker)
        if cooldown_until is not None:
            # Blokada ponownego wejścia po stop-lossie (patrz komentarz przy
            # STOP_LOSS_REENTRY_COOLDOWN) - zapobiega whipsaw-owi typu IFXd_EQ
            # 28-29.07 (10 wejść/stop-lossów w 15 minut na tym samym tickerze).
            diagnostics.log_diag(
                user_id, "signal",
                f"{ticker_display_name(asset.ticker)}: pominięte wejście - cooldown po stop-lossie do {cooldown_until.strftime('%Y-%m-%d %H:%M')} UTC.",
            )
            continue
        other = market_hours.held_by_other_engine(user_id, asset.ticker, "signal")
        if other is not None:
            # Cudza pozycja (Micro-Grid/EOD) na tym samym tickerze - pomijamy,
            # zeby nie powtorzyc kolizji SUp_EQ (patrz market_hours.py::
            # held_by_other_engine).
            diagnostics.log_diag(
                user_id, "signal",
                f"{ticker_display_name(asset.ticker)}: pominięte wejście - już otwarte w {other}.",
            )
            continue
        sector_collision = sector_diversity.held_sector_ticker(user_id, env, asset.ticker)
        if sector_collision is not None:
            # Koncentracja sektorowa (2026-08-10) - patrz docstring
            # sector_diversity.py, znalezione na żywo (Sygnał, 4/6 pozycji w
            # tym samym sektorze). Skanuje wszystkie 3 silniki.
            diagnostics.log_diag(
                user_id, "signal",
                f"{ticker_display_name(asset.ticker)}: pominięte wejście - już otwarta pozycja w tym samym sektorze ({ticker_display_name(sector_collision)}).",
            )
            continue
        if not market_hours.is_market_open(asset.currency):
            continue

        candles = price_feed.get_mini_chart_ohlc(
            api_key, asset.ticker, days=SIGNAL_LOOKBACK_DAYS,
            alpaca_api_key=alpaca_key, alpaca_api_secret=alpaca_secret,
        )
        if not candles or len(candles) < MA_PERIOD:
            continue

        closes = [Decimal(str(c["c"])) for c in candles]
        rsi = _compute_rsi(closes)
        sma = _compute_sma(closes)
        atr = _compute_atr(candles)
        if rsi is None or sma is None or atr is None:
            continue

        price = price_feed.get_live_price(api_key, asset.ticker, alpaca_key, alpaca_secret, ibkr_host, ibkr_port)
        if price is None or price <= 0:
            continue

        if rsi < settings.rsi_threshold and price > sma:
            score = settings.rsi_threshold - rsi
            candidates.append((asset, score, price, atr))

    if not candidates:
        return

    candidates.sort(key=lambda c: c[1], reverse=True)
    _log(
        user_id, "INFO",
        f"Wejścia: {len(candidates)} kandydat(ów) spełnia warunek. Najlepszy: "
        f"{candidates[0][0].ticker} (RSI-score {candidates[0][1]:.2f}).",
    )
    for asset, _score, price, atr in candidates:
        if _enter_position(user_id, client, asset, settings, price, atr, current_equity):
            return


def _confirm_pending_entries(user_id: int, client: T212Client, settings: SignalSettings, pending_order_ids: set[str]) -> None:
    """
    Sprawdza świeżo złożone zlecenia kupna (buy_confirmed=False) - gdy
    zniknęły z pending, uzbraja stop-loss (POJEDYNCZY resting STOP, patrz
    docstring modułu). Ten sam wzorzec co bot_engine.py przy potwierdzaniu
    wejścia Micro-Grid, uproszczony (brak DCA, brak trailing exitu).

    `pending_order_ids` pobierane RAZ w tick() i dzielone z _manage_exits -
    unika dublowania get_pending_orders w tym samym ticku (patrz
    TICK_ERROR_BACKOFF_MINUTES wyżej, ten sam powód co w bot_engine.py).
    """
    pending_trades = SignalTrade.query.filter_by(
        user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=False,
        environment=current_environment(user_id),
    ).all()
    if not pending_trades:
        return

    now = dt.datetime.utcnow()
    for trade in pending_trades:
        if trade.buy_order_id in pending_order_ids:
            continue  # wciąż czeka na wypełnienie

        backoff_key = (user_id, trade.ticker)
        backoff = _confirm_fail_backoff.get(backoff_key)
        if backoff is not None and now < backoff[1]:
            continue  # w backoffie po poprzednich błędach get_position, patrz CONFIRM_FAIL_BACKOFF_MINUTES

        try:
            position = client.get_position(trade.ticker)
        except T212APIError as exc:
            consecutive = (backoff[0] if backoff else 0) + 1
            delay = _next_confirm_fail_delay(consecutive)
            _confirm_fail_backoff[backoff_key] = (consecutive, now + delay)
            _log(
                user_id, "ERROR",
                f"{ticker_display_name(trade.ticker)}: błąd sprawdzenia portfela po zakupie #{consecutive} z rzędu - {exc} - "
                f"kolejna próba za {int(delay.total_seconds() // 60)} min.",
            )
            continue
        _confirm_fail_backoff.pop(backoff_key, None)

        current_owned = Decimal(str(position["quantity"])) if position else Decimal("0")
        filled = current_owned - trade.baseline_owned_quantity
        if filled <= 0:
            trade.status = "CLOSED"
            trade.closed_via = "never-filled"
            trade.closed_at = dt.datetime.utcnow()
            db.session.commit()
            _log(user_id, "ERROR", f"{ticker_display_name(trade.ticker)}: zlecenie kupna zniknęło z kolejki bez wypełnienia (anulowane/odrzucone).")
            continue

        trade.quantity = filled
        trade.allocated_value = filled * trade.buy_price
        trade.buy_confirmed = True
        db.session.commit()

        try:
            stop_result = client.place_stop_order(trade.ticker, -filled, trade.stop_loss_price)
            trade.stop_order_id = stop_result.order_id
            db.session.commit()
            # KRYTYCZNE: dopisz nowo uzbrojony stop do TEGO SAMEGO pending_order_ids,
            # ktorego zaraz potem uzyje _manage_exits w tym samym ticku (patrz tick()/
            # reconcile() - dzielony snapshot, TICK_ERROR_BACKOFF_MINUTES wyzej).
            # Bez tego _manage_exits widzialby swiezo zlozony stop jako "nieobecny w
            # pending" (bo snapshot pobrano PRZED tym place'em) i falszywie uznawal
            # pozycje za zamknieta w tym samym ticku, w ktorym dopiero co ja otworzyl -
            # zywy bug znaleziony 2026-07-28 na IFXd_EQ: 12 cykli kupno->"zamkniecie"
            # w <1s kazdy, bot kupowal od nowa co ~60s bo _process_entries widzial
            # brak otwartej pozycji, mimo ze prawdziwe zlecenie/akcje caly czas
            # zostawaly na T212 (zadna prawdziwa sprzedaz nigdy nie poszla -
            # _finalize_closed_trade tylko zmienia lokalny status, nie wola T212) -
            # stad realne konto skonczylo z ~19 naskladanymi akcjami zamiast 1.
            pending_order_ids.add(stop_result.order_id)
            _log(user_id, "INFO", f"{ticker_display_name(trade.ticker)}: kupno potwierdzone ({filled} szt.), stop-loss uzbrojony na {trade.stop_loss_price:.4f}.")
        except T212APIError as exc:
            _log(
                user_id, "ERROR",
                f"{ticker_display_name(trade.ticker)}: kupno potwierdzone, ale NIE udało się uzbroić stop-loss - {exc}. "
                "Pozycja NIECHRONIONA żadnym resting orderem, sprawdź ręcznie.",
            )


def _bump_buy_retry(user_id: int, trade: SignalTrade, reason: str) -> None:
    trade.buy_retry_count += 1
    trade.next_buy_retry_at = dt.datetime.utcnow() + _next_retry_delay(trade.buy_retry_count)
    db.session.commit()
    _log(user_id, "INFO", f"{ticker_display_name(trade.ticker)}: sprawdzenie LIMIT BUY #{trade.buy_retry_count} - {reason}")


def _retry_pending_buys(
    user_id: int, client: T212Client, settings: SignalSettings, pending: list[dict],
) -> None:
    """
    "Goni" cenę LIMIT BUY, który jeszcze się nie wypełnił i przy obecnej
    cenie rynkowej JUŻ SIĘ NIE MOŻE wypełnić (rynek odjechał POWYŻEJ limitu -
    LIMIT BUY z definicji nigdy nie wykona się drożej niż jego limit) -
    anuluje stare zlecenie i wystawia nowe po aktualnej cenie, żeby pozycja
    nie czekała w nieskończoność. Dokładny port `bot_engine.py::
    _retry_pending_buys` (Micro-Grid) - dodane 2026-07-30 po realnym
    znalezisku: IFXd_EQ utknęło na 9+ godzin (limit 55,18€, cena uciekła do
    59,47€, ~8% wyżej), Sygnał do tej pory nie miał ŻADNEGO mechanizmu
    ponawiania (w odróżnieniu od Micro-Gridu, który ma to od 2026-07-21).

    Przeliczanie ilości od DOCELOWEJ kwoty alokacji (SignalAsset.entry_amount),
    NIE od starej `trade.quantity` - ten sam fix co w bot_engine.py 2026-07-22
    (Meta/FB: stara, 14x niższa cena rozdęła pozycję z 25 USD do 354 USD,
    bo kod trzymał starą ilość i tylko podmieniał cenę).

    `pending`: już pobrana lista z get_pending_orders[_for_tick]() - dzielona
    z `_confirm_pending_entries` w tym samym cyklu (rate limit demo ciasny).

    stop_loss_only_mode (2026-08-06) - patrz identyczny guard i uzasadnienie
    w bot_engine.py::_retry_pending_buys (znalezione na żywo na Micro-Gridzie,
    ten sam mechanizm/ryzyko tutaj).
    """
    if settings.stop_loss_only_mode:
        return
    now = dt.datetime.utcnow()
    candidates = (
        SignalTrade.query
        .filter_by(
            user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=False,
            environment=current_environment(user_id),
        )
        .filter(db.or_(SignalTrade.next_buy_retry_at.is_(None), SignalTrade.next_buy_retry_at <= now))
        .all()
    )
    candidates = [t for t in candidates if market_hours.is_position_management_hours(t.currency)]
    if not candidates:
        return

    pending_by_id = {str(o.get("id")): o for o in pending}
    market_keys = get_decrypted_market_data_keys(user_id, bot_credentials.get_master_key(user_id))

    for trade in candidates:
        pending_order = pending_by_id.get(trade.buy_order_id)
        if pending_order is None:
            continue  # zniknęło z pending - zajmie się tym _confirm_pending_entries

        if Decimal(str(pending_order.get("filledQuantity", 0))) > 0:
            continue  # częściowo już wypełnione - nie anulujemy w połowie, niech dokończy

        current_price = price_feed.get_live_price(
            market_keys.get("finnhub_api_key"), trade.ticker,
            market_keys.get("alpaca_api_key"), market_keys.get("alpaca_api_secret"),
            market_keys.get("ibkr_host"), market_keys.get("ibkr_port"),
        )
        if current_price is None or current_price <= 0:
            _bump_buy_retry(user_id, trade, "brak aktualnej ceny do porównania z limitem, spróbuję ponownie.")
            continue

        if current_price <= trade.buy_price:
            # Limit wciąż marketable (cena nie odjechała w górę) - zwyczajnie
            # jeszcze się nie wykonało, nic do gonienia.
            _bump_buy_retry(
                user_id, trade,
                f"cena ({current_price}) wciąż <= limitu ({trade.buy_price}), czekam na wypełnienie.",
            )
            continue

        asset = SignalAsset.query.get(trade.signal_asset_id)
        target_amount = asset.entry_amount if asset is not None else (trade.quantity * trade.buy_price)
        new_quantity = (target_amount / current_price).quantize(Decimal("0.0001"))
        if new_quantity <= 0:
            _bump_buy_retry(
                user_id, trade,
                f"cena odjechała ({current_price} > limit {trade.buy_price}), ale przeliczona ilość <= 0 "
                f"(kwota {target_amount} / cena {current_price}), pomijam ten tick.",
            )
            continue

        try:
            client.cancel_order(trade.buy_order_id)
        except T212APIError as exc:
            _bump_buy_retry(
                user_id, trade,
                f"cena odjechała ({current_price} > limit {trade.buy_price}), ale anulowanie starego "
                f"zlecenia nie powiodło się - {exc}",
            )
            continue

        try:
            new_result, new_quantity = _place_buy_with_precision_fallback(
                client, trade.ticker, new_quantity, current_price,
            )
        except T212APIError as exc:
            _bump_buy_retry(
                user_id, trade,
                f"stare LIMIT BUY anulowane, ale nowe po {current_price} nie powiodło się - {exc}",
            )
            continue

        old_price = trade.buy_price
        old_quantity = trade.quantity
        trade.buy_order_id = new_result.order_id
        trade.buy_price = current_price
        trade.quantity = new_quantity
        trade.allocated_value = (new_quantity * current_price).quantize(Decimal("0.01"))
        trade.buy_retry_count = 0
        trade.next_buy_retry_at = None
        db.session.commit()
        _log(
            user_id, "INFO",
            f"{ticker_display_name(trade.ticker)}: cena odjechała ({old_price} -> {current_price}) - LIMIT BUY ponowiony po nowej "
            f"cenie, ilość przeliczona ({old_quantity} -> {new_quantity}) żeby zachować alokację ~{target_amount}.",
        )


# Ulamek jednego ATR - jak duza musi byc poprawa zanim oplaca sie placic
# Cancel-Replace z ciasnego rate limitu demo T212 (ten sam powod co
# MIN_TRAIL_REQUOTE_FRACTION w bot_engine.py, tylko liczony wzgledem ATR
# zamiast wzgledem take_profit_step_pct - Sygnal nie ma odpowiednika tego
# ostatniego).
MIN_TRAIL_REQUOTE_ATR_FRACTION = Decimal("0.1")


def _trail_stop_loss(
    user_id: int, client: T212Client, trade: SignalTrade, settings: SignalSettings, price: Decimal,
) -> None:
    """
    Przesuwa stop-loss W GÓRĘ (nigdy w dół) gdy bieżąca cena pozwala na
    ciaśniejszy poziom niż obecny - dodane 2026-07-28, patrz uzasadnienie w
    docstringu modułu ("TRAILING STOP-LOSS"). Ten sam dystans co przy
    wejściu (`atr_at_entry * stop_loss_atr_mult`), liczony od BIEŻĄCEJ ceny -
    naturalnie aktywuje się dopiero gdy pozycja jest na plusie względem
    wejścia, więc chroni WYŁĄCZNIE już zarobiony zysk.

    Wołane z `_manage_exits()` na KAŻDYM ticku dla każdej otwartej pozycji -
    sztywny take-profit usunięty 2026-07-28 (patrz docstring modułu), więc
    nie ma już "sufitu", po którym pozycja sama się zamyka - trailing SL to
    jedyny mechanizm wyjścia poza ręcznym zamknięciem.
    """
    candidate_stop = signal_strategy.compute_trailing_stop(
        trade.stop_loss_price, price, trade.atr_at_entry, settings.stop_loss_atr_mult,
    )
    unprotected = trade.stop_order_id is None
    if candidate_stop is None:
        if not unprotected:
            return  # brak ATR z wejscia, albo nic do poprawy (stop juz na tym poziomie albo wyzej)
        # BRAK aktywnego stopu na koncie (np. po CANCELLED/nie-FILLED wykrytym w
        # _manage_exits) a cena nie daje zadnej poprawy - "brak poprawy" != "jest
        # ochrona". Bez tego pozycja zostaje bez stopu w nieskonczonosc, dopoki
        # cena akurat sama nie da powodu do requote'u (Merlin Properties/
        # MRLe_EQ, 18-21.08.2026: 3 dni bez SL). Wystawiamy OSTATNI znany poziom.
        candidate_stop = trade.stop_loss_price

    if not unprotected:
        min_requote_threshold = trade.stop_loss_price + (trade.atr_at_entry * MIN_TRAIL_REQUOTE_ATR_FRACTION)
        if candidate_stop < min_requote_threshold:
            return  # poprawa za mala zeby placic Cancel-Replace'em z ciasnego rate limitu

    # Cudzy (nie-botowy) SELL na tickerze - patrz identyczny komentarz i
    # incydent w bot_engine.py::_manage_trailing_exit (2026-08-10, Adam po
    # TotalEnergies/SUp_EQ). get_pending_orders() dzieli 50s cache z resztą
    # ticku, więc to zwykle cache hit, nie dodatkowy request.
    try:
        live_orders = client.get_pending_orders()
    except T212APIError:
        live_orders = []
    foreign_order = next(
        (o for o in live_orders if o.get("ticker") == trade.ticker and o.get("side") == "SELL"), None,
    )
    if foreign_order is not None and str(foreign_order.get("id")) != trade.stop_order_id:
        trade.stop_order_id = None
        trade.status = "RELEASED"
        db.session.commit()
        _log(
            user_id, "WARN",
            f"{ticker_display_name(trade.ticker)}: wykryto na T212 zlecenie SELL ({foreign_order.get('id')}) spoza bota "
            f"(initiatedFrom={foreign_order.get('initiatedFrom')}) - pozycja zwolniona spod zarządzania "
            "automatycznie, żeby bot nie dobijał się o nią co tick. Udziały zostają na koncie.",
        )
        return

    if trade.stop_order_id:
        try:
            client.cancel_order(trade.stop_order_id)
        except T212APIError as exc:
            # Mogl sie wlasnie wykonac rownolegle (wyscig z T212) - kolejny
            # tick wykryje to jako zamkniecie (zniknie z pending). Nie
            # probujemy wystawic nowego stopu na pozycje ktora juz mogla
            # przestac istniec.
            _log(user_id, "INFO", f"{ticker_display_name(trade.ticker)}: przesunięcie trailing stop-loss - anulowanie starego ({trade.stop_order_id}) nie powiodło się (mógł się już wykonać) - {exc}")
            return

    try:
        stop_result = client.place_stop_order(trade.ticker, -trade.quantity, candidate_stop)
    except T212APIError as exc:
        trade.stop_order_id = None
        # owned:0 - pozycja sprzedana poza botem, ale bez wiszącego zlecenia w
        # pending (ręczna sprzedaż już się dawno wykonała) - sam check "cudze
        # SELL w pending" wyżej tego nie łapie (patrz identyczny fix i incydent
        # Heineken/HEIAa_EQ w bot_engine.py::_manage_trailing_exit, 2026-08-21).
        if _extract_owned_quantity_from_error(exc) == 0:
            trade.status = "RELEASED"
            db.session.commit()
            _log(
                user_id, "WARN",
                f"{ticker_display_name(trade.ticker)}: T212 zgłasza owned:0 przy próbie uzbrojenia STOP-a - "
                "pozycja sprzedana poza botem, zwolniona spod zarządzania automatycznie, żeby bot nie "
                "dobijał się o nią co tick.",
            )
            return
        db.session.commit()
        _log(user_id, "ERROR", f"{ticker_display_name(trade.ticker)}: uzbrojenie przesuniętego stop-loss (target {candidate_stop}) nie powiodło się - {exc}. Pozycja NIECHRONIONA do następnego ticku.")
        return

    old_stop = trade.stop_loss_price
    trade.stop_loss_price = candidate_stop
    trade.stop_order_id = stop_result.order_id
    db.session.commit()
    _log(user_id, "INFO", f"{ticker_display_name(trade.ticker)}: trailing stop-loss przesunięty z {old_stop:.4f} na {candidate_stop:.4f} (cena teraz {price:.4f}).")


def _manage_exits(
    user_id: int, client: T212Client, settings: SignalSettings,
    pending_order_ids: set[str], pending_fetch_ok: bool,
) -> None:
    open_trades = SignalTrade.query.filter_by(
        user_id=user_id, status="OPEN", is_paper=False, buy_confirmed=True,
        sl_suspended_for_weekend=False, environment=current_environment(user_id),
    ).all()
    if not open_trades:
        return

    # `pending_order_ids`/`pending_fetch_ok` pobierane RAZ w tick() (dzielone
    # z _confirm_pending_entries, patrz TICK_ERROR_BACKOFF_MINUTES wyżej) -
    # pending_fetch_ok=False (błąd LUB tick w backoffie) wyłącza WYŁĄCZNIE
    # detekcję "czy stop już sam się wykonał" niżej, reszta pętli (trailing
    # stop-loss - ochrona zysku, patrz
    # [[feedback_snajper_profit_protection_priority]]) leci dalej normalnie,
    # bo nie potrzebuje pending wcale (tylko ceny z Finnhub/Alpaca/Yahoo).
    market_keys = get_decrypted_market_data_keys(user_id, bot_credentials.get_master_key(user_id))
    api_key = market_keys.get("finnhub_api_key")
    alpaca_key = market_keys.get("alpaca_api_key")
    alpaca_secret = market_keys.get("alpaca_api_secret")
    ibkr_host = market_keys.get("ibkr_host")
    ibkr_port = market_keys.get("ibkr_port")

    for trade in open_trades:
        if not market_hours.is_position_management_hours(trade.currency):
            continue

        # Stop-loss zniknal z pending - MOZE oznaczac wykonanie, ale samo
        # zniknięcie tego NIE dowodzi (mogło też zostać anulowane/odrzucone
        # przez T212) - ten sam bug i fix co bot_engine.py::_resolve_vanished_leg
        # (znaleziony tam 2026-07-23), nigdy nie przeniesiony do Sygnału.
        # Sprawdzamy realny status w historii T212 PRZED uznaniem za zamknięte.
        if pending_fetch_ok and trade.stop_order_id and trade.stop_order_id not in pending_order_ids:
            item = _lookup_recent_order(client, trade.stop_order_id)
            if item is None:
                # Jeszcze nie wiadomo (historia nie nadążyła/błąd) - sprawdzimy
                # ponownie następnym razem, nic nie zmieniamy teraz.
                continue
            status = (item.get("order") or {}).get("status")
            if status == _FILLED_ORDER_STATUS:
                fill_price_raw = (item.get("fill") or {}).get("price")
                fill_price = Decimal(str(fill_price_raw)) if fill_price_raw is not None else trade.stop_loss_price
                _finalize_closed_trade(user_id, trade, "stop-loss", fill_price=fill_price)
            else:
                # Anulowane/odrzucone, NIE wykonane - pozycja zostaje OPEN,
                # noga zostanie wystawiona od nowa niżej (stop_order_id=None).
                _log(
                    user_id, "WARN",
                    f"{ticker_display_name(trade.ticker)}: zlecenie stop-loss {trade.stop_order_id} zniknęło z pending, ale "
                    f"historia T212 pokazuje status {status} (NIE {_FILLED_ORDER_STATUS}) - NIE zamykam "
                    "pozycji, zlecenie zostanie wystawione ponownie.",
                )
                trade.stop_order_id = None
                db.session.commit()
            continue

        price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret, ibkr_host, ibkr_port)
        if price is None or price <= 0:
            # Watchdog - patrz identyczny komentarz w bot_engine.py::_manage_trailing_exit.
            if price_watchdog.note_price_result("signal", trade.ticker, False):
                _log(
                    user_id, "ERROR",
                    f"{ticker_display_name(trade.ticker)}: brak ceny przez {price_watchdog.ALERT_THRESHOLD} ticków z rzędu - "
                    "trailing stop NIE działa dla tej pozycji! Sprawdź TICKER_MAP/mapowanie Yahoo "
                    "(finnhub_client.py) albo pokrycie instrumentu.",
                )
            continue
        price_watchdog.note_price_result("signal", trade.ticker, True)
        position_alerts.check_move_alert(user_id, "signal", trade.ticker, trade.buy_price, price, trade.currency)

        _trail_stop_loss(user_id, client, trade, settings, price)


def _trail_stop_loss_paper(trade: SignalTrade, settings: SignalSettings, price: Decimal) -> None:
    """Jak _trail_stop_loss(), ale bez T212 (pozycja papierowa - czysty zapis do bazy, zero zlecen/rate limitu)."""
    candidate_stop = signal_strategy.compute_trailing_stop(
        trade.stop_loss_price, price, trade.atr_at_entry, settings.stop_loss_atr_mult,
    )
    if candidate_stop is None:
        return
    trade.stop_loss_price = candidate_stop
    db.session.commit()


def _manage_paper_exits(user_id: int, settings: SignalSettings) -> None:
    """Pozycje papierowe nie maja zadnego zlecenia na T212 - stop-loss (w tym trailing) sprawdzany WYLACZNIE tutaj, w softwarze."""
    open_trades = SignalTrade.query.filter_by(
        user_id=user_id, status="OPEN", is_paper=True, environment=current_environment(user_id),
    ).all()
    if not open_trades:
        return

    market_keys = get_decrypted_market_data_keys(user_id, bot_credentials.get_master_key(user_id))
    api_key = market_keys.get("finnhub_api_key")
    alpaca_key = market_keys.get("alpaca_api_key")
    alpaca_secret = market_keys.get("alpaca_api_secret")
    ibkr_host = market_keys.get("ibkr_host")
    ibkr_port = market_keys.get("ibkr_port")

    for trade in open_trades:
        if not market_hours.is_position_management_hours(trade.currency):
            continue
        price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret, ibkr_host, ibkr_port)
        if price is None or price <= 0:
            continue
        if price <= trade.stop_loss_price:
            _finalize_closed_trade(user_id, trade, "stop-loss", fill_price=price)
        else:
            _trail_stop_loss_paper(trade, settings, price)


def reconcile(user_id: int) -> None:
    """Wołane raz zaraz po aktywacji (routes/signal.py::activate) - nadgania stan bez czekania na najbliższy tick co 60s."""
    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        _log(user_id, "ERROR", "Reconciliation: brak poświadczeń w bot_credentials mimo aktywacji.")
        return
    creds = get_decrypted_credentials(user_id, master_key, current_environment(user_id))
    if creds is None:
        _log(user_id, "ERROR", "Reconciliation: brak zapisanego klucza API demo, pomijam.")
        return
    settings = SignalSettings.query.filter_by(user_id=user_id).first()
    if not settings or not settings.is_active:
        # Symetrycznie do tick() - patrz identyczny fix w bot_engine.py
        # (2026-08-09, IDEAS_v2.md TODO z 2026-08-06).
        return

    _manage_paper_exits(user_id, settings)
    if not settings.is_paper_trading:
        client = T212Client(
            api_key=creds["api_key"], api_secret=creds["api_secret"], environment=current_environment(user_id),
            engine="signal", user_id=user_id,
        )
        # get_pending_orders_for_tick() zamiast get_pending_orders() - reconcile()
        # (start appki + ręczna aktywacja) wcześniej omijało WSPÓLNY backoff/cache
        # tick()/reconcile() między Micro-Grid/Sygnał/EOD (patrz t212_client.py),
        # więc restart appki albo ręczne "Aktywuj" mogło strzelić realny request
        # do T212 W TRAKCIE aktywnego backoffu ustawionego przez inny silnik, a
        # własny 429 stąd nie zasilał eskalacji - złapane na żywo 2026-08-04.
        try:
            pending = client.get_pending_orders_for_tick()
        except T212APIError as exc:
            consecutive, delay_seconds = client.tick_backoff_status() or (1, 60.0)
            _log(
                user_id, "ERROR",
                f"Reconciliation: błąd pobierania pending orders #{consecutive} z rzędu ({exc}) - "
                f"kolejna próba za {max(1, round(delay_seconds / 60))} min.",
            )
            pending_ids = set()
            pending_fetch_ok = False
        else:
            if pending is None:
                _log(user_id, "INFO", "Reconciliation: pomijam pending orders (wspólny backoff po wcześniejszych błędach T212).")
                pending_ids = set()
                pending_fetch_ok = False
            else:
                pending_ids = {str(o.get("id")) for o in pending}
                pending_fetch_ok = True
        if pending_fetch_ok:
            _confirm_pending_entries(user_id, client, settings, pending_ids)
            _retry_pending_buys(user_id, client, settings, pending=pending)
        _manage_exits(user_id, client, settings, pending_order_ids=pending_ids, pending_fetch_ok=pending_fetch_ok)


def tick(app) -> None:
    """Wołane cyklicznie przez APScheduler co 60s (patrz app/__init__.py) - ten sam wzorzec co bot_engine.py::tick."""
    with app.app_context():
        for user_id in bot_credentials.active_user_ids():
            settings = SignalSettings.query.filter_by(user_id=user_id).first()
            if not settings or not settings.is_active:
                continue

            diagnostics.log_diag(user_id, "signal", "tick start")

            _manage_paper_exits(user_id, settings)

            if settings.is_paper_trading:
                # Wejscia papierowe nie dotykaja T212 wcale (patrz _enter_position -
                # sprawdza is_paper_trading PRZED jakimkolwiek uzyciem client), stad
                # bezpieczne None zamiast prawdziwego T212Client. Okno wejscia
                # sprawdzane per-aktywo/waluta wewnatrz _process_entries.
                if not settings.stop_loss_only_mode:
                    current_equity = _get_current_equity(user_id, settings) if settings.equity_sizing_enabled else None
                    _process_entries(user_id, None, settings, current_equity)
                continue

            master_key = bot_credentials.get_master_key(user_id)
            if master_key is None:
                continue
            creds = get_decrypted_credentials(user_id, master_key, current_environment(user_id))
            if creds is None:
                _log(user_id, "ERROR", "Brak zapisanego klucza API demo, pomijam tick.")
                continue
            client = T212Client(
            api_key=creds["api_key"], api_secret=creds["api_secret"], environment=current_environment(user_id),
            engine="signal", user_id=user_id,
        )

            # Kupno nie ma priorytetu (Adam, 2026-07-27, patrz ten sam komentarz
            # w bot_engine.py::tick) - w backoffie po błędach get_pending_orders
            # pomijamy TYLKO nowe wejścia i potwierdzanie/detekcję przez pending,
            # _manage_exits (trailing stop-loss) leci zawsze, patrz wyżej.
            skip_new_entries = False
            # get_pending_orders_for_tick() = get_pending_orders() + backoff
            # WSPÓLNY między Micro-Grid/Sygnał/EOD, patrz t212_client.py - None
            # = wciąż w backoffie po poprzednich błędach (JAKIEGOKOLWIEK z
            # trzech silników).
            try:
                pending = client.get_pending_orders_for_tick()
            except T212APIError as exc:
                consecutive, delay_seconds = client.tick_backoff_status() or (1, 60.0)
                _log(
                    user_id, "ERROR",
                    f"Tick: błąd pobierania pending orders #{consecutive} z rzędu ({exc}) - "
                    f"kolejna próba za {max(1, round(delay_seconds / 60))} min zamiast za 60s.",
                )
                skip_new_entries = True
                _manage_exits(user_id, client, settings, pending_order_ids=set(), pending_fetch_ok=False)
            else:
                if pending is None:
                    skip_new_entries = True
                    _manage_exits(user_id, client, settings, pending_order_ids=set(), pending_fetch_ok=False)
                else:
                    pending_ids = {str(o.get("id")) for o in pending}
                    _confirm_pending_entries(user_id, client, settings, pending_ids)
                    _retry_pending_buys(user_id, client, settings, pending=pending)
                    _manage_exits(user_id, client, settings, pending_order_ids=pending_ids, pending_fetch_ok=True)

            if skip_new_entries or settings.stop_loss_only_mode:
                continue
            current_equity = _get_current_equity(user_id, settings, client) if settings.equity_sizing_enabled else None
            _process_entries(user_id, client, settings, current_equity)
