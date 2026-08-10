"""
migrate_add_weekend_sl_suspension.py
=======================================
Jednorazowa migracja: dokłada kolumnę `sl_suspended_for_weekend` (domyślnie
0/False) do active_trades/signal_trades/eod_trades - patrz
app/services/weekend_guard.py (Adam, 2026-08-10: "zdjąć SL od piątku 21:00
do poniedziałku 11:00, boję się luki na otwarciu USA po weekendzie").

Odróżnia "pozycja świadomie zawieszona na weekend" (stop_order_id=None,
sl_suspended_for_weekend=True) od "pozycja jeszcze nigdy nie uzbrojona"
(stop_order_id=None, sl_suspended_for_weekend=False) - bez tego rozróżnienia
zwykły tick w trakcie okna zawieszenia próbowałby natychmiast uzbroić nowy
stop, cofając całą blokadę w ciągu 60s (patrz _trail_stop_loss/
_manage_trailing_exit w każdym z 3 silników - wołają place_stop_order
bezwarunkowo, gdy tylko stop_order_id jest puste).

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumna już istnieje).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_weekend_sl_suspension.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "sniper.db"
TABLES = ("active_trades", "signal_trades", "eod_trades")


def main() -> None:
    if not DB_PATH.exists():
        print(f"Nie znaleziono bazy: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for table in TABLES:
        cols = [row[1] for row in cur.execute(f"PRAGMA table_info({table})")]
        if "sl_suspended_for_weekend" in cols:
            print(f"{table}.sl_suspended_for_weekend już istnieje - nic do zrobienia.")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN sl_suspended_for_weekend BOOLEAN NOT NULL DEFAULT 0")
            conn.commit()
            print(f"OK - dodano kolumnę {table}.sl_suspended_for_weekend (domyślnie 0/False).")

    conn.close()


if __name__ == "__main__":
    main()
