"""
app/services/telegram_commands.py
====================================
Obsługa wiadomości PRZYCHODZĄCYCH z Telegrama - na razie tylko `/status`
(Adam, 2026-08-05: "dodaj /status"). W odróżnieniu od telegram_notify.py
(czysty push w jedną stronę) to jest polling (getUpdates) wołany cyklicznie
przez APScheduler (patrz app/__init__.py, ~co 15s) - Telegram Bot API nie
ma tu webhooka (appka nie ma publicznego HTTPS), więc krótki poll jest
prostszym, wystarczającym rozwiązaniem przy jednym użytkowniku.

Offset (który update_id już przetworzony) trzymany w pamięci procesu -
restart zeruje go, co oznacza że PIERWSZY poll po restarcie może dostać
"stare" nieprzetworzone wiadomości sprzed restartu i na nie odpowie - to
akceptowalne (user i tak dostanie odpowiedź, tylko z opóźnieniem), dużo
prostsze niż przeżywanie restartu przez plik/bazę dla czegoś tak niskiego
ryzyka.

BEZPIECZEŃSTWO: odpowiada WYŁĄCZNIE na wiadomości z chat_id zgodnego z
TELEGRAM_CHAT_ID w configu - każdy inny nadawca (gdyby ktoś znalazł nazwę
bota) jest cicho ignorowany, nie dostaje żadnej odpowiedzi ani nie
uruchamia żadnej logiki.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytz
from flask import current_app

from ..extensions import db
from ..models import (
    ActiveTrade, BotAuditLog, EODAuditLog, EODSettings, EODTrade,
    RiskSettings, SignalAuditLog, SignalSettings, SignalTrade,
)
from ..routes.api_keys import get_decrypted_credentials
from ..utils import current_environment, humanize_ticker_prefix, telegram_env_tag, ticker_display_name as _display_name
from . import bot_credentials, bot_engine, daily_summary, eod_engine, price_feed, signal_engine, telegram_notify, weekend_guard
from .t212_client import T212APIError, T212Client

# Pozycje czekające na potwierdzenie /close (Adam, 2026-08-09) - "tak" w
# ciągu _CLOSE_CONFIRM_TTL_MINUTES wykonuje sprzedaż. None jako ticker =
# "all". Bez tego jeden przypadkowy/zdublowany request do Telegrama mógłby
# sprzedać żywą pozycję bez żadnej siatki bezpieczeństwa.
_pending_close: dict[int, tuple[str | None, dt.datetime]] = {}
_CLOSE_CONFIRM_TTL_MINUTES = 2

# Silniki + ich tabela audytu dla /why (Adam, 2026-08-09: "raport dlaczego
# bot nie kupił") - patrz bot_engine.py/signal_engine.py/eod_engine.py::
# _log_block_reason_throttled, tam gdzie te wpisy powstają.
_WHY_ENGINES = (("Micro-Grid", BotAuditLog), ("Sygnał", SignalAuditLog), ("EOD", EODAuditLog))
_WHY_KEYWORDS = ("pominięte", "limit", "Wejścia:")
_WHY_WINDOW_HOURS = 2

# Mapowanie słowa-argumentu -> (nazwa wyświetlana, model ustawień, atrybut
# is_active) dla /pause i /resume (Adam, 2026-08-09). Świadomie NIE reużywa
# routes/*.py::activate()/deactivate() - te dodatkowo gaszą/zbroją współdzielone
# bot_credentials (patrz routes/bot.py:558-638) i activate() wymaga hasła do
# odszyfrowania klucza, którego Telegram nie ma. /pause ma być odwracalnym,
# lekkim przełącznikiem flagi - broker/poświadczenia zostają nietknięte.
_ENGINE_SETTINGS = {
    "bot": ("Micro-Grid", RiskSettings, "is_bot_active", BotAuditLog),
    "signal": ("Sygnał", SignalSettings, "is_active", SignalAuditLog),
    "eod": ("EOD", EODSettings, "is_active", EODAuditLog),
}

_last_update_id: int | None = None
_AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")

ENGINE_ICONS = {"Micro-Grid": "🔷", "Sygnał": "⚡", "EOD": "🌙"}

# /help (Adam, 2026-08-09) - statyczny tekst, ręcznie aktualizowany przy
# każdej nowej komendzie (celowo NIE generowany z docstringów - te są pisane
# dla programisty, nie dla szybkiego odczytu na telefonie).
_HELP_TEXT = """🤖 SNAJPER - dostępne komendy

/status - stan kont (equity, P&L, otwarte pozycje) teraz
/status 7d - to samo, zrealizowany P&L z ostatnich 7 dni (dowolna liczba dni)
/why - dlaczego bot nie kupił (ostatnie 2h zablokowanych wejść)

/pause bot|signal|eod - pauzuje silnik (nie dotyka brokera/poświadczeń)
/resume bot|signal|eod - wznawia spauzowany silnik

/close TICKER - zamyka pozycję (Market Sell) po potwierdzeniu
/close all - zamyka WSZYSTKIE otwarte pozycje po potwierdzeniu
tak - potwierdza ostatni /close (2 min na odpowiedź, potem wygasa)

/slpause - zdejmuje SL ze wszystkich pozycji już teraz (auto: pt 21:00)
/slresume - przywraca SL już teraz (auto: pon 11:00)

kupiłem - potwierdza ręczne kupno po sygnale, przekazuje botowi
nie - odrzuca aktualnie sugerowany sygnał wejścia na 5 min

/help - ta wiadomość"""


def _status_message(user_id: int, days: int = 1) -> str:
    """
    ZMIANA 2026-08-05 (Adam: "status mam ale zero konkretow typu strata zysk
    otwarte pozycje itd") - pierwsza wersja pokazywała tylko aktywny/nie +
    LICZBĘ otwartych pozycji, bez samych kwot. Teraz reużywa
    daily_summary._engine_pnl_24h/_account_total (ten sam kod co poranne/
    wieczorne podsumowanie) - jedno zapytanie T212 o equity całego konta
    (bezpieczne, /status jest wołane na żądanie, nie co tick) + realized/
    unrealized per silnik + rozpiska KAŻDEJ otwartej pozycji z jej P&L.

    `days` (Adam, 2026-08-09: "/status 7d") - okno zrealizowanego P&L, patrz
    poll_and_handle() gdzie parsowany jest argument z treści komendy.
    """
    now = dt.datetime.now(_AMSTERDAM_TZ).strftime("%d.%m %H:%M")
    window_label = "teraz" if days == 1 else f"ostatnie {days}d"
    lines = [f"🤖 [{telegram_env_tag(user_id)}] SNAJPER STATUS ({window_label}, {now})", ""]

    account_total = daily_summary._account_total(user_id)
    if account_total is not None:
        from ..models import UserSettings
        user_settings = UserSettings.query.filter_by(user_id=user_id).first()
        baseline = user_settings.account_baseline_equity if user_settings else None
        line = f"Całość konta: {account_total:.2f}€"
        if baseline:
            pnl = account_total - baseline
            pct = (pnl / baseline * 100) if baseline > 0 else 0
            line += f" (vs start {baseline:.0f}€: {pnl:+.2f}€, {pct:+.1f}%)"
        lines.append(line)
        lines.append("")

    def _engine_section(name: str, settings, is_active_attr: str, trade_model) -> None:
        icon = ENGINE_ICONS.get(name, "")
        if settings is None:
            lines.append(f"{icon} {name}: nieskonfigurowany")
            return
        is_active = getattr(settings, is_active_attr)
        status_icon = "✅ aktywny" if is_active else "⛔ wyłączony"
        data = daily_summary._engine_pnl_24h(user_id, trade_model, days=days)
        lines.append(
            f"{icon} {name}: {status_icon} — zrealizowane {window_label if days != 1 else '24h'} {data['realized']:+.2f}€ "
            f"({data['realized_n']} zamkniętych), niezrealizowane {data['unrealized']:+.2f}€ "
            f"({data['open_n']} otwartych)"
        )
        for ticker, pnl, currency in data["open_positions"]:
            company = _display_name(ticker)
            if pnl is None:
                lines.append(f"  ⚪ {company}: brak ceny")
            else:
                pnl_icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
                lines.append(f"  {pnl_icon} {company}: {pnl:+.2f} {currency}")

    risk = RiskSettings.query.filter_by(user_id=user_id).first()
    _engine_section("Micro-Grid", risk, "is_bot_active", ActiveTrade)
    lines.append("")
    signal = SignalSettings.query.filter_by(user_id=user_id).first()
    _engine_section("Sygnał", signal, "is_active", SignalTrade)
    lines.append("")
    eod = EODSettings.query.filter_by(user_id=user_id).first()
    _engine_section("EOD", eod, "is_active", EODTrade)

    return "\n".join(lines)


def _why_message(user_id: int) -> str:
    """
    Adam, 2026-08-09: "raport dlaczego bot nie kupił" - czyta ostatnie
    _WHY_WINDOW_HOURS z 3 audit logów (per silnik), filtruje po słowach
    kluczowych z _log_block_reason_throttled (bot_engine.py i analogi w
    signal_engine.py/eod_engine.py) - nie instrumentujemy KAŻDEGO cichego
    'continue' (np. 'giełda zamknięta' jest oczywiste), tylko te 3
    wartościowe powody (limit pozycji, cudza pozycja, backoff).
    """
    env = current_environment(user_id)
    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=_WHY_WINDOW_HOURS)
    lines = [f"🔍 [{telegram_env_tag(user_id)}] Dlaczego bot nie kupił (ostatnie {_WHY_WINDOW_HOURS}h)", ""]

    for name, model in _WHY_ENGINES:
        rows = (
            model.query
            .filter_by(user_id=user_id, environment=env)
            .filter(model.created_at >= cutoff)
            .order_by(model.created_at.desc())
            .limit(50)
            .all()
        )
        matched = [r for r in rows if any(k in r.message for k in _WHY_KEYWORDS)][:10]
        icon = ENGINE_ICONS.get(name, "")
        lines.append(f"{icon} {name}:")
        if not matched:
            lines.append("  (brak zablokowanych wejść)")
        else:
            for r in matched:
                local_time = pytz.utc.localize(r.created_at).astimezone(_AMSTERDAM_TZ).strftime("%H:%M")
                lines.append(f"  {local_time} — {humanize_ticker_prefix(r.message)}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _ticker_query_matches(trade_ticker: str, query: str) -> bool:
    """
    Dopasowanie /close TICKER - Adam nie zna/nie dba o dokładny format
    tickera z giełdowym sufiksem (np. "QIAd_EQ") - w praktyce wpisuje krótki
    symbol ("QIAD", "qiad") albo nazwę spółki (2026-08-09: "nie widzi tickera
    ... na demo qiad" - sam pełny "QIAd_EQ" zadziałałby, ale nikt tak nie
    pisze). Dopasowuje po kolei: pełny ticker, prefiks przed "_" (giełdowy
    sufiks odcięty), nazwa spółki - wszystko case-insensitive.
    """
    q = query.strip().lower()
    if trade_ticker.lower() == q:
        return True
    if trade_ticker.split("_")[0].lower() == q:
        return True
    return _display_name(trade_ticker).lower() == q


def _find_open_trades(user_id: int, env: str, ticker: str | None = None) -> list[tuple[str, object]]:
    """
    Zwraca [(nazwa_silnika, trade)] otwartych pozycji, opcjonalnie tylko dla
    jednego tickera (patrz _ticker_query_matches) - patrz /close w
    poll_and_handle(). Przeszukuje po kolei wszystkie 3 silniki - w praktyce
    co najwyżej jedna OPEN pozycja na ticker w całym systemie na raz (patrz
    market_hours.py::held_by_other_engine, wołane przy każdym wejściu), więc
    nie trzeba position_group_id do disambiguacji.
    """
    found: list[tuple[str, object]] = []
    for name, model in (("Micro-Grid", ActiveTrade), ("Sygnał", SignalTrade), ("EOD", EODTrade)):
        for trade in model.query.filter_by(user_id=user_id, status="OPEN", environment=env).all():
            if ticker is not None and not _ticker_query_matches(trade.ticker, ticker):
                continue
            found.append((name, trade))
    return found


def _close_trade(engine_name: str, trade) -> tuple[bool, str]:
    """
    Dispatch do właściwej implementacji zamknięcia wg silnika. Sygnał/EOD
    mają wspólną close_trade_manual (signal_engine.py/eod_engine.py,
    wydzieloną z routes/*.py::close_position 2026-08-09) - Micro-Grid nie ma
    odpowiednika (jego _finalize_closed_trade ma inny kształt, patrz
    bot_engine.py::close_active_trade_manual), więc klienta T212 budujemy
    tu, tym samym wzorcem co bot_engine.py::adopt_confirmed_signal.
    """
    if engine_name == "Sygnał":
        return signal_engine.close_trade_manual(trade.user_id, trade)
    if engine_name == "EOD":
        return eod_engine.close_trade_manual(trade.user_id, trade)

    user_id = trade.user_id
    name = _display_name(trade.ticker)
    api_key = current_app.config.get("FINNHUB_API_KEY")
    alpaca_key = current_app.config.get("ALPACA_API_KEY")
    alpaca_secret = current_app.config.get("ALPACA_API_SECRET")
    price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret)

    master_key = bot_credentials.get_master_key(user_id)
    if master_key is None:
        return False, f"{name}: Micro-Grid nie ma aktywnych poświadczeń."
    env = current_environment(user_id)
    creds = get_decrypted_credentials(user_id, master_key, env)
    if creds is None:
        return False, f"{name}: brak zapisanego klucza API."
    client = T212Client(
        api_key=creds["api_key"], api_secret=creds["api_secret"], environment=env,
        engine="bot", user_id=user_id,
    )
    try:
        bot_engine.close_active_trade_manual(user_id, client, trade, price or trade.buy_price)
    except T212APIError as exc:
        return False, f"{name}: sprzedaż Market nie powiodła się - {exc}."
    return True, f"{name}: zamknięte @ ~{price or trade.buy_price}."


def poll_and_handle(app) -> None:
    """Wołane cyklicznie przez APScheduler (patrz app/__init__.py::_register_scheduler)."""
    global _last_update_id

    with app.app_context():
        token = app.config.get("TELEGRAM_BOT_TOKEN")
        chat_id = app.config.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return

        offset = _last_update_id + 1 if _last_update_id is not None else None
        updates = telegram_notify.get_updates(token, offset)
        if not updates:
            return

        from ..models import User

        for update in updates:
            _last_update_id = update["update_id"]
            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            sender_chat_id = str((message.get("chat") or {}).get("id", ""))

            if sender_chat_id != str(chat_id):
                continue  # nie Adam - cicho ignorujemy, patrz docstring modułu

            if text in ("/help", "/start"):
                # "/start" dorzucone (Adam, 2026-08-09) - to jedyna wiadomość
                # jaką Telegram wysyła automatycznie przy pierwszym otwarciu
                # czatu z nowym botem, niech od razu pokaże listę komend
                # zamiast ciszy.
                telegram_notify.send_telegram_message(token, chat_id, _HELP_TEXT)

            elif text == "/status" or text.startswith("/status "):
                # "/status 7d" (Adam, 2026-08-09) - okno zrealizowanego P&L
                # zamiast domyślnego "teraz" (days=1). Argument nieparsowalny
                # -> cicho wraca do domyślnego 1, nie warto błędować o to.
                days = 1
                arg = text.split(maxsplit=1)[1].strip().rstrip("dD") if " " in text else ""
                if arg.isdigit():
                    days = int(arg)
                # Jednoosobowa appka w praktyce (patrz bot_engine.py::daily_report,
                # ten sam wzorzec) - pierwszy user z jakimkolwiek RiskSettings.
                risk = RiskSettings.query.first()
                if risk is None:
                    telegram_notify.send_telegram_message(token, chat_id, "Brak skonfigurowanego bota.")
                    continue
                user = User.query.get(risk.user_id)
                if user is None:
                    continue
                telegram_notify.send_telegram_message(token, chat_id, _status_message(user.id, days=days))

            elif text == "/why":
                risk = RiskSettings.query.first()
                if risk is None:
                    continue
                telegram_notify.send_telegram_message(token, chat_id, _why_message(risk.user_id))

            elif text.lower() == "/close" or text.lower().startswith("/close "):
                # "/close TICKER" albo "/close all" (Adam, 2026-08-09) - NIE
                # sprzedaje od razu, tylko staguje w _pending_close i czeka
                # na "tak" (patrz niżej) - prawdziwe pieniądze na PROD, jeden
                # przypadkowy telegramowy retry nie może sprzedać pozycji.
                risk = RiskSettings.query.first()
                if risk is None:
                    continue
                user_id = risk.user_id
                env = current_environment(user_id)
                arg = text.split(maxsplit=1)[1].strip() if " " in text else ""
                if arg.lower() == "all":
                    matches = _find_open_trades(user_id, env)
                    pending_ticker = None
                elif arg:
                    matches = _find_open_trades(user_id, env, ticker=arg)
                    pending_ticker = arg
                else:
                    telegram_notify.send_telegram_message(token, chat_id, "Użycie: /close TICKER albo /close all.")
                    continue

                if not matches:
                    telegram_notify.send_telegram_message(token, chat_id, "Nie znalazłem otwartej pozycji do zamknięcia.")
                    continue

                api_key = current_app.config.get("FINNHUB_API_KEY")
                alpaca_key = current_app.config.get("ALPACA_API_KEY")
                alpaca_secret = current_app.config.get("ALPACA_API_SECRET")
                lines = ["⚠️ Na pewno zamknąć (Market Sell)?", ""]
                for name, trade in matches:
                    price = price_feed.get_live_price(api_key, trade.ticker, alpaca_key, alpaca_secret)
                    cost_basis = getattr(trade, "average_price", None) or trade.buy_price
                    pnl_txt = ""
                    if price:
                        pnl = (Decimal(str(price)) - cost_basis) * trade.quantity
                        pnl_txt = f", P/L ~{pnl:+.2f} {trade.currency}"
                    icon = ENGINE_ICONS.get(name, "")
                    lines.append(f"{icon} {_display_name(trade.ticker)} ({name}) @ ~{price or cost_basis}{pnl_txt}")
                lines.append("")
                lines.append(f"Odpisz 'tak' w ciągu {_CLOSE_CONFIRM_TTL_MINUTES} min, żeby potwierdzić.")
                _pending_close[user_id] = (pending_ticker, dt.datetime.utcnow() + dt.timedelta(minutes=_CLOSE_CONFIRM_TTL_MINUTES))
                telegram_notify.send_telegram_message(token, chat_id, "\n".join(lines))

            elif text.lower() in ("tak", "tak."):
                risk = RiskSettings.query.first()
                if risk is None:
                    continue
                user_id = risk.user_id
                pending = _pending_close.pop(user_id, None)
                if pending is None or dt.datetime.utcnow() > pending[1]:
                    telegram_notify.send_telegram_message(token, chat_id, "Nie ma nic do potwierdzenia (albo czas na potwierdzenie minął).")
                    continue
                pending_ticker, _expires = pending
                env = current_environment(user_id)
                matches = _find_open_trades(user_id, env, ticker=pending_ticker)
                if not matches:
                    telegram_notify.send_telegram_message(token, chat_id, "Pozycja już nie jest otwarta (zamknięta wcześniej?).")
                    continue
                results = []
                for name, trade in matches:
                    ok, msg = _close_trade(name, trade)
                    results.append(("✅ " if ok else "⚠️ ") + msg)
                telegram_notify.send_telegram_message(token, chat_id, "\n".join(results))

            elif text.lower().startswith("/pause ") or text.lower().startswith("/resume "):
                command, _, engine_word = text.partition(" ")
                entry = _ENGINE_SETTINGS.get(engine_word.strip().lower())
                if entry is None:
                    telegram_notify.send_telegram_message(
                        token, chat_id, "Nieznany silnik - użyj: bot / signal / eod.",
                    )
                    continue
                name, model, attr, audit_model = entry
                risk = RiskSettings.query.first()
                if risk is None:
                    continue
                settings = model.query.filter_by(user_id=risk.user_id).first()
                if settings is None:
                    telegram_notify.send_telegram_message(token, chat_id, f"{name}: nieskonfigurowany.")
                    continue
                resume = command.lower() == "/resume"
                setattr(settings, attr, resume)
                # Dopisane 2026-08-09 (Adam próbował zweryfikować pauzę/wznowienie
                # "w logach" - do tej pory /pause i /resume NIC nie logowały,
                # więc nie było jak sprawdzić bez odpytania bazy) - jeden wpis
                # do Dziennika danego silnika, ten sam wzorzec co inne akcje z
                # Telegrama (patrz np. adopt_confirmed_signal).
                db.session.add(audit_model(
                    user_id=risk.user_id, action_type="INFO",
                    message=f"Silnik {'wznowiony' if resume else 'spauzowany'} przez Telegram.",
                    environment=current_environment(risk.user_id),
                ))
                db.session.commit()
                icon = ENGINE_ICONS.get(name, "")
                state = "wznowiony ✅" if resume else "spauzowany ⛔"
                telegram_notify.send_telegram_message(token, chat_id, f"{icon} {name}: {state}.")

            elif text.lower() == "/slpause":
                # Ręczny override harmonogramu weekendowego (Adam, 2026-08-10 -
                # patrz weekend_guard.py) - normalnie automatyczne pt 21:00/
                # pon 11:00, ale Adam może chcieć zdjąć SL wcześniej (np. przed
                # ważnym newsem w tygodniu). Wysyła własne potwierdzenie na
                # Telegram (patrz suspend_all), więc tu nic dodatkowego.
                weekend_guard.suspend_all(app)

            elif text.lower() == "/slresume":
                weekend_guard.restore_all(app)

            elif text.lower() in ("kupiłem", "kupilem", "kupiłam", "kupilam"):
                # Potwierdzenie ręcznego kupna po sygnale (Adam, 2026-08-07:
                # "bot dał sygnał, kupiłem, niech on to zrozumie słowo
                # kupiłem") - patrz bot_engine.py::adopt_confirmed_signal,
                # to samo co przycisk "Przekaż botowi", tylko z Telegrama.
                risk = RiskSettings.query.first()
                if risk is None:
                    continue
                ok, msg = bot_engine.adopt_confirmed_signal(risk.user_id)
                telegram_notify.send_telegram_message(token, chat_id, ("✅ " if ok else "⚠️ ") + msg)

            elif text.lower() in ("nie", "nie.", "no"):
                # Odrzucenie aktualnie sugerowanego sygnału wejścia (Adam,
                # 2026-08-07) - patrz bot_engine.py::reject_current_signal.
                risk = RiskSettings.query.first()
                if risk is None:
                    continue
                rejected = bot_engine.reject_current_signal(risk.user_id)
                if rejected is None:
                    telegram_notify.send_telegram_message(token, chat_id, "Nie było żadnego świeżego sygnału do odrzucenia.")
                else:
                    telegram_notify.send_telegram_message(
                        token, chat_id,
                        f"OK, nie podpowiem {rejected} przez {bot_engine.ENTRY_SIGNAL_REJECT_MINUTES} min.",
                    )
