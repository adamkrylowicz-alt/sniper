"""
migrate_add_max_concurrent_positions.py
========================================
Jednorazowa migracja: dokłada kolumnę max_concurrent_positions na trzech
tabelach ustawień (risk_settings/signal_settings/eod_settings), przenosząc
limit jednoczesnych otwartych pozycji ze stałych modułowych
(bot_engine.MAX_CONCURRENT_POSITIONS, signal_engine.MAX_CONCURRENT_POSITIONS)
do ustawień per-user, edytowalnych w UI (Adam, 2026-08-03: "to tylko
ustawienia fabryczne", ma się dać zmieniać bez redeployu).

Domyślne wartości (zachowują dotychczasowe zachowanie / ustalone 02-03.08):
- risk_settings.max_concurrent_positions   = 6 (Micro-Grid, bez zmiany)
- signal_settings.max_concurrent_positions = 2 (Sygnał, bez zmiany)
- eod_settings.max_concurrent_positions    = 2 (EOD, NOWY limit - wcześniej brak)

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny już istnieją).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_max_concurrent_positions.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "sniper.db"

COLUMNS = [
    ("risk_settings", "max_concurrent_positions", 6),
    ("signal_settings", "max_concurrent_positions", 2),
    ("eod_settings", "max_concurrent_positions", 2),
]


def main() -> None:
    if not DB_PATH.exists():
        print(f"Nie znaleziono bazy: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for table, column, default in COLUMNS:
        cols = [row[1] for row in cur.execute(f"PRAGMA table_info({table})")]
        if column in cols:
            print(f"{table}.{column} już istnieje - nic do zrobienia.")
            continue
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER NOT NULL DEFAULT {default}")
        conn.commit()
        print(f"OK - dodano kolumnę {table}.{column} (domyślnie {default}).")

    conn.close()


if __name__ == "__main__":
    main()
