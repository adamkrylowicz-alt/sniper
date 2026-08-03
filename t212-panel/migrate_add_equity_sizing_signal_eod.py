"""
migrate_add_equity_sizing_signal_eod.py
=========================================
Jednorazowa migracja: rozszerza money management √equity (dotąd tylko
Micro-Grid, patrz migrate_add_equity_sizing.py z 2026-07-31) na Sygnał i EOD
(Adam, 2026-08-03: "dodaj do obu") - dokłada te same dwie kolumny na
signal_settings/eod_settings:

- equity_sizing_enabled (domyślnie 0/False - zero zmiany zachowania dopóki
  ktoś świadomie nie zaznaczy checkboxa w UI)
- equity_sizing_baseline (NULL dopóki nikt nie włączył flagi - auto-capture
  przez serwer przy włączeniu, patrz routes/signal.py, routes/eod.py)

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny już istnieją).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_equity_sizing_signal_eod.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "sniper.db"

TABLES = ["signal_settings", "eod_settings"]


def main() -> None:
    if not DB_PATH.exists():
        print(f"Nie znaleziono bazy: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for table in TABLES:
        cols = [row[1] for row in cur.execute(f"PRAGMA table_info({table})")]

        if "equity_sizing_enabled" in cols:
            print(f"{table}.equity_sizing_enabled już istnieje - nic do zrobienia.")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN equity_sizing_enabled BOOLEAN NOT NULL DEFAULT 0")
            conn.commit()
            print(f"OK - dodano kolumnę {table}.equity_sizing_enabled (domyślnie 0/False).")

        if "equity_sizing_baseline" in cols:
            print(f"{table}.equity_sizing_baseline już istnieje - nic do zrobienia.")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN equity_sizing_baseline NUMERIC(12, 2)")
            conn.commit()
            print(f"OK - dodano kolumnę {table}.equity_sizing_baseline (NULL - brak dopóki nikt nie włączy flagi).")

    conn.close()


if __name__ == "__main__":
    main()
