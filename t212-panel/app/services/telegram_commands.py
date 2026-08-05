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

import pytz

from ..models import ActiveTrade, EODSettings, EODTrade, Instrument, RiskSettings, SignalSettings, SignalTrade
from ..utils import friendly_name
from . import daily_summary, telegram_notify

_last_update_id: int | None = None
_AMSTERDAM_TZ = pytz.timezone("Europe/Amsterdam")

ENGINE_ICONS = {"Micro-Grid": "🔷", "Sygnał": "⚡", "EOD": "🌙"}


def _display_name(ticker: str) -> str:
    """
    Nazwa spółki zamiast surowego tickera T212 (Adam, 2026-08-05: "wysylalo
    nazwy a nie tickery... nie rozeznaje sie za bardzo") - ten sam wzorzec co
    reszta appki (routes/*.py::friendly_name, ucina formalne końcówki typu
    "Inc"/"SE"). Fallback na surowy ticker gdy instrument nie jest w
    lokalnym cache (nie powinno się zdarzyć dla własnej listy bota, ale bez
    zgadywania).
    """
    instrument = Instrument.query.get(ticker)
    if instrument is None or not instrument.name:
        return ticker
    return friendly_name(instrument.name) or ticker


def _status_message(user_id: int) -> str:
    """
    ZMIANA 2026-08-05 (Adam: "status mam ale zero konkretow typu strata zysk
    otwarte pozycje itd") - pierwsza wersja pokazywała tylko aktywny/nie +
    LICZBĘ otwartych pozycji, bez samych kwot. Teraz reużywa
    daily_summary._engine_pnl_24h/_account_total (ten sam kod co poranne/
    wieczorne podsumowanie) - jedno zapytanie T212 o equity całego konta
    (bezpieczne, /status jest wołane na żądanie, nie co tick) + realized/
    unrealized per silnik + rozpiska KAŻDEJ otwartej pozycji z jej P&L.
    """
    now = dt.datetime.now(_AMSTERDAM_TZ).strftime("%d.%m %H:%M")
    lines = [f"🤖 SNAJPER STATUS ({now})", ""]

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
        data = daily_summary._engine_pnl_24h(user_id, trade_model)
        lines.append(
            f"{icon} {name}: {status_icon} — zrealizowane 24h {data['realized']:+.2f}€ "
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

            if text == "/status":
                # Jednoosobowa appka w praktyce (patrz bot_engine.py::daily_report,
                # ten sam wzorzec) - pierwszy user z jakimkolwiek RiskSettings.
                risk = RiskSettings.query.first()
                if risk is None:
                    telegram_notify.send_telegram_message(token, chat_id, "Brak skonfigurowanego bota.")
                    continue
                user = User.query.get(risk.user_id)
                if user is None:
                    continue
                telegram_notify.send_telegram_message(token, chat_id, _status_message(user.id))
