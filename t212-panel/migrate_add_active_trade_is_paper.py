"""
migrate_add_active_trade_is_paper.py
======================================
Jednorazowa migracja: dokłada kolumnę active_trades.is_paper na istniejącej
bazie (instance/sniper.db). Backfill = False (0) dla wszystkich istniejących
wierszy - każda pozycja sprzed tej migracji jest realnym zleceniem na T212
(bot ignorował is_paper_trading do tej pory, patrz bot_engine.py).

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumna już istnieje).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_active_trade_is_paper.py
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
    if "is_paper" in cols:
        print("is_paper już istnieje w active_trades - nic do zrobienia.")
        conn.close()
        return

    cur.execute("ALTER TABLE active_trades ADD COLUMN is_paper INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()
    print("OK - dodano active_trades.is_paper (0/False dla wszystkich istniejacych wierszy - to byly realne zlecenia).")


if __name__ == "__main__":
    main()
