"""
app/services/position_alerts.py
===================================
Alert na Telegram gdy otwarta pozycja poruszy się o >= MOVE_ALERT_THRESHOLD
od ceny kupna, zamiast czekać na poranne/wieczorne podsumowanie (Adam,
2026-08-09: "alert przy dużym ruchu pozycji"). Wołane WEWNĄTRZ istniejących
pętli trailing-exit (bot_engine.py::_manage_trailing_exit i analogi w
signal_engine.py/eod_engine.py) - tam gdzie żywa cena jest już pobrana, więc
zero dodatkowych zapytań do Finnhub/Alpaca/Yahoo.

Dedupe wzorowany na price_watchdog.py (odpal raz, wymagaj powrotu pod próg
zanim znów) - inaczej Telegram zalałby się identycznym alertem co tick
(60s) przez cały czas gdy pozycja trzyma się za progiem.

2026-08-28: `_alerted` żyje TYLKO w pamięci procesu (restart appki = pusty
set) - znalezione na żywo, PayPal (Micro-Grid, DEV) siedział ~13% pod ceną
kupna od dłuższego czasu, każdy restart procesu (2x tego dnia przy okazji
niezwiązanej poprawki resolvera) natychmiast odpalał ŚWIEŻY alert Telegram
o tym samym, już dawno zgłoszonym ruchu - Adam dostał 3-4 identyczne alerty
o PayPalu bez żadnego nowego ruchu ceny. Fix: `_seen` (osobny set, nigdy nie
czyszczony przez discard) - pierwszy check danego klucza w życiu procesu
TYLKO zapisuje stan (`_alerted`), bez wysyłki, jeśli już jest za progiem -
appka "cicho" zakłada że o istniejącym przekroczeniu progu Adam już
wie/dowie się z /status, zamiast zgadywać czy powiadomienie poszło przed
restartem. Kolejne PRAWDZIWE przekroczenia w tym samym życiu procesu nadal
alarmują normalnie - to celowo tylko gasi fałszywy alarm restartowy, nie
tłumi żadnego realnego nowego ruchu.
"""

from __future__ import annotations

from decimal import Decimal

from flask import current_app

from ..utils import should_notify_environment, telegram_env_tag, ticker_display_name
from . import telegram_notify

MOVE_ALERT_THRESHOLD = Decimal("0.05")  # ±5%

_alerted: set[tuple[str, str]] = set()  # (engine, ticker)
_seen: set[tuple[str, str]] = set()  # (engine, ticker) - czy juz sprawdzany w tym zyciu procesu


def check_move_alert(user_id: int, engine: str, ticker: str, buy_price, current_price, currency: str) -> None:
    if not buy_price or buy_price <= 0 or current_price is None:
        return
    buy_price = Decimal(str(buy_price))
    price = Decimal(str(current_price))
    move_pct = (price - buy_price) / buy_price
    key = (engine, ticker)

    if abs(move_pct) < MOVE_ALERT_THRESHOLD:
        _alerted.discard(key)  # wróciła pod próg - kolejne wybicie znów zaalarmuje
        _seen.add(key)
        return
    if key in _alerted:
        return  # już zaalarmowane, cisza dopóki nie wróci pod próg (patrz wyżej)
    first_check_this_process = key not in _seen
    _seen.add(key)
    _alerted.add(key)
    if first_check_this_process:
        return  # pierwszy tick po (re)starcie - cicho zapisz stan, nie alarmuj od nowa

    if not should_notify_environment(user_id):
        return
    direction = "🟢 w górę" if move_pct > 0 else "🔴 w dół"
    name = ticker_display_name(ticker)
    telegram_notify.send_telegram_message(
        current_app.config.get("TELEGRAM_BOT_TOKEN"), current_app.config.get("TELEGRAM_CHAT_ID"),
        f"📈 [{telegram_env_tag(user_id)}] {name}: {direction} {move_pct * 100:+.1f}% "
        f"({(price - buy_price):+.2f} {currency}) od kupna @ {buy_price:.2f} -> teraz {price:.2f}.",
    )
