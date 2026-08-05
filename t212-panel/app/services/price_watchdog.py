"""
app/services/price_watchdog.py
================================
Wykrywa CISZĘ zamiast błędu - gdy get_live_price() konsekwentnie zwraca
None dla tickera w zarządzaniu WYJŚCIEM (trailing stop) z otwartej pozycji,
poprzedni kod (bot_engine.py::_manage_trailing_exit i odpowiedniki w
signal_engine.py/eod_engine.py::_manage_exits) po prostu robił `continue`
BEZ ŻADNEGO loga. Znalezione na żywo 2026-08-05: TICKER_MAP["RHMd_EQ"] miał
zły symbol Yahoo (".XETRA" zamiast ".DE") od dnia adopcji pozycji - ponad
30 godzin, ~30+ ticków, trailing stop NIGDY się nie uzbroił, i ANI JEDEN
log (ani plik błędów, ani Telegram, ani diagnostyka) o tym nie wspominał,
bo formalnie nic "nie rzuciło wyjątku" - po prostu cicho nic się nie działo.

Ten moduł liczy kolejne nieudane próby per (silnik, ticker) w pamięci
procesu (restart zeruje - to monitoring, nie stan biznesowy) i sygnalizuje
DOKŁADNIE RAZ, gdy streak przekracza próg - wywołujący loguje wtedy ERROR
(co automatycznie leci też na Telegram, patrz _log() w każdym silniku).
Po tym jednym alarmie milczy dalej co tick (żeby nie zasypać Telegrama tym
samym komunikatem co minutę), aż cena znów się pojawi (streak resetuje się
do zera) - kolejna cisza od nowa dostanie kolejny, świeży alarm.
"""

from __future__ import annotations

ALERT_THRESHOLD = 10  # ~10 ticków (przy 60s tick to ok. 10 minut) bez ceny, zanim zaalarmujemy

_streaks: dict[tuple[str, str], int] = {}


def note_price_result(engine: str, ticker: str, price_found: bool) -> bool:
    """
    Zwraca True DOKŁADNIE w ticku, w którym streak braku ceny przekroczył
    próg (wywołujący powinien wtedy zalogować ERROR) - False we wszystkich
    innych przypadkach (cena jest, streak jeszcze pod progiem, albo streak
    już dawno przekroczył próg i alarm już poleciał wcześniej).
    """
    key = (engine, ticker)
    if price_found:
        _streaks.pop(key, None)
        return False
    count = _streaks.get(key, 0) + 1
    _streaks[key] = count
    return count == ALERT_THRESHOLD
