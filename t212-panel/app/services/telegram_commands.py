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

from ..models import ActiveTrade, EODSettings, EODTrade, RiskSettings, SignalSettings, SignalTrade
from . import telegram_notify

_last_update_id: int | None = None


def _status_message(user_id: int) -> str:
    now = dt.datetime.now().strftime("%d.%m %H:%M")
    lines = [f"🤖 SNAJPER STATUS ({now})", ""]

    risk = RiskSettings.query.filter_by(user_id=user_id).first()
    if risk is not None:
        n = ActiveTrade.query.filter_by(user_id=user_id, status="OPEN").count()
        lines.append(f"Micro-Grid: {'✅ aktywny' if risk.is_bot_active else '⛔ wyłączony'}, {n} otwartych pozycji")
    else:
        lines.append("Micro-Grid: nieskonfigurowany")

    signal = SignalSettings.query.filter_by(user_id=user_id).first()
    if signal is not None:
        n = SignalTrade.query.filter_by(user_id=user_id, status="OPEN").count()
        lines.append(f"Sygnał: {'✅ aktywny' if signal.is_active else '⛔ wyłączony'}, {n} otwartych pozycji")
    else:
        lines.append("Sygnał: nieskonfigurowany")

    eod = EODSettings.query.filter_by(user_id=user_id).first()
    if eod is not None:
        n = EODTrade.query.filter_by(user_id=user_id, status="OPEN").count()
        lines.append(f"EOD: {'✅ aktywny' if eod.is_active else '⛔ wyłączony'}, {n} otwartych pozycji")
    else:
        lines.append("EOD: nieskonfigurowany")

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
