"""
migrate_add_active_trade_baseline_qty.py
==========================================
Jednorazowa migracja: dokłada kolumnę active_trades.baseline_owned_quantity
na istniejącej bazie (instance/sniper.db) - patrz
services/bot_engine.py::_attempt_sell_placement.

Backfill dla wierszy sprzed tej migracji: 0 - bezpieczny domyślny stan (te
pozycje albo są już CLOSED, albo trzeba je i tak ręcznie ogarnąć skoro nie
mają tej wartości ustawionej poprawnie od początku).

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumna już istnieje).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_active_trade_baseline_qty.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "sniper.db"


def main() -> None:
    if not DB_PATH.exists():
        print(f"Nie znaleziono bazy: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cols = [row[1] for row in cur.execute("PRAGMA table_info(active_trades)")]
    if "baseline_owned_quantity" in cols:
        print("baseline_owned_quantity już istnieje w active_trades - nic do zrobienia.")
        conn.close()
        return

    cur.execute("ALTER TABLE active_trades ADD COLUMN baseline_owned_quantity NUMERIC(12, 4) NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()
    print("OK - dodano active_trades.baseline_owned_quantity (0 dla wszystkich istniejacych wierszy).")


if __name__ == "__main__":
    main()
