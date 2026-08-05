"""
app/services/telegram_notify.py
=================================
Wysyłka powiadomień na Telegram (Adam, 2026-08-05: "zrob jakis port do
telegrama zeby mi wysylal takie bledy a nie milczal jak po smierci
organisty" - po znalezieniu buga RHMd_EQ, który 30+ godzin siedział
CAŁKOWICIE po cichu, zero śladu nawet w bot_errors.log).

Bot Telegrama: @Snajper2026_bot (utworzony przez BotFather).
TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID w .env - gdy brak (niekonfigurowane),
send_telegram_message() cicho nic nie robi (fail-safe, appka ma działać
identycznie bez tego jak z tym).

ŚWIADOMA DECYZJA: awaria samego Telegrama (sieć, zły token, limit) NIGDY
nie może wywrócić ticku bota - stąd `except Exception` (nie tylko
requests.RequestException) i brak `raise` gdziekolwiek w tym module.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_TMPL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_UPDATES_TMPL = "https://api.telegram.org/bot{token}/getUpdates"
REQUEST_TIMEOUT = 8


def send_telegram_message(token: str | None, chat_id: str | None, text: str) -> bool:
    """
    Zwraca True przy sukcesie, False przy niepowodzeniu (brak konfiguracji,
    błąd sieci, odrzucenie przez Telegram) - wywołujący NIE powinien nic z
    tym robić poza (opcjonalnie) zalogowaniem, na pewno nie przerywać ticku.
    """
    if not token or not chat_id:
        return False
    try:
        resp = requests.post(
            TELEGRAM_API_TMPL.format(token=token),
            data={"chat_id": chat_id, "text": text},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("Telegram: HTTP %s przy wysyłce powiadomienia", resp.status_code)
            return False
        return True
    except Exception as exc:  # celowo szeroko - Telegram nigdy nie może wywalić ticku bota
        logger.warning("Telegram: błąd wysyłki powiadomienia: %s", exc)
        return False


def get_updates(token: str | None, offset: int | None = None) -> list[dict]:
    """
    Odpytuje Telegram o nowe wiadomości przychodzące (patrz
    services/telegram_commands.py - obsługa /status). `offset` = update_id
    ostatnio przetworzonej wiadomości + 1 (Telegram nie zwraca już
    potwierdzonych - to jego wbudowany mechanizm "przeczytane", nie trzeba
    nic pamiętać po stronie Telegrama, tylko przekazywać rosnący offset).

    `timeout=0` (krótki poll, nie long-poll) - wołane cyklicznie przez
    APScheduler co ~15s (patrz app/__init__.py), więc nie ma sensu trzymać
    otwartego połączenia - kolejny poll i tak przyjdzie za chwilę.

    Zwraca pustą listę przy jakimkolwiek błędzie (fail-safe, jak
    send_telegram_message) - brak nowych wiadomości NIE różni się od błędu
    sieci z punktu widzenia wywołującego, oba po prostu "nic teraz nie rób".
    """
    if not token:
        return []
    try:
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        resp = requests.get(TELEGRAM_UPDATES_TMPL.format(token=token), params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("Telegram: HTTP %s przy odczycie wiadomości", resp.status_code)
            return []
        return resp.json().get("result", [])
    except Exception as exc:  # celowo szeroko, ten sam powod co send_telegram_message
        logger.warning("Telegram: błąd odczytu wiadomości: %s", exc)
        return []
