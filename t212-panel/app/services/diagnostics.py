"""
app/services/diagnostics.py
=============================
Ukryty log diagnostyczny (2026-07-30, na prośbę Adama po sesji debugowania
DTEd_EQ/rate-limitu/DCA - "dodaj ukryty na potrzeby testów i wewnętrznej
diagnostyki") - CELOWO osobny od bot_audit_log/signal_audit_log/eod_audit_log
(Dziennik w UI) i od bot_engine.py::BOT_ERROR_LOG_FILENAME. Filozofia
ISTNIEJĄCYCH logów (co widzi user w UI, co ląduje w bot_errors.log) zostaje
BEZ ZMIAN - ten plik jest czystym DODATKIEM, nigdy nie zastępuje niczego.

Zapisuje WSZYSTKO co normalnie i tak trafia do _log() w każdym z 3 silników
(niezależnie od action_type/gdzie POZA TYM ląduje), PLUS zdarzenia, których
dotąd nigdzie nie było widać na żywo (real request do T212 - metoda/path/
status/rate-limit remaining, cache hit vs świeże zapytanie, start/koniec
ticku per silnik) - dokładnie te rzeczy, których dzisiaj brakowało przy
ręcznej diagnozie 429/DTEd i które normalnie trzeba było rekonstruować z
wielu źródeł naraz (DB + bot_errors.log + realne zapytanie do T212).

Nigdy nie rzuca wyjątku dalej - diagnostyka nie może wywrócić tick()u.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from flask import current_app

DIAGNOSTICS_LOG_FILENAME = "bot_diagnostics.log"

_throttle_notice_at: dict[tuple[int, str], dt.datetime] = {}


def should_log_throttled(user_id: int, key: str, cooldown_minutes: int = 30) -> bool:
    """
    Dedupe dla powtarzających się powodów blokady wejścia (limit pozycji,
    cudza pozycja, backoff) które normalnie zalałyby log identycznym wpisem
    co tick (bot_engine.py/eod_engine.py/signal_engine.py::_process_entries,
    dodane 2026-08-09 dla raportu /why na Telegramie - patrz
    telegram_commands.py). Zwraca True (i zapamiętuje moment) raz na
    `cooldown_minutes` per (user_id, key), potem False aż do odnowienia
    okna. Scalone tu 2026-08-09 z 3 identycznych kopii w każdym silniku -
    WOŁAJĄCY musi sam prefiksować `key` nazwą silnika (np. "bot:max_concurrent")
    żeby dzielony słownik nie mylił tego samego klucza z różnych silników.
    """
    now = dt.datetime.utcnow()
    notice_key = (user_id, key)
    last = _throttle_notice_at.get(notice_key)
    if last is not None and now - last < dt.timedelta(minutes=cooldown_minutes):
        return False
    _throttle_notice_at[notice_key] = now
    return True


def log_diag(user_id: int | None, engine: str, message: str) -> None:
    """
    `engine`: "bot" (Micro-Grid) / "signal" / "eod" - albo "t212" dla wpisów
    z t212_client.py, gdzie nie zawsze wiadomo który silnik akurat woła
    (np. reconcile() wywołany spoza tick()u). `user_id=None` dozwolone z
    tego samego powodu.
    """
    try:
        path = Path(current_app.instance_path) / DIAGNOSTICS_LOG_FILENAME
        timestamp = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        user_part = user_id if user_id is not None else "?"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} UTC | user={user_part} | [{engine}] {message}\n")
        path.chmod(0o600)
    except Exception:
        pass
