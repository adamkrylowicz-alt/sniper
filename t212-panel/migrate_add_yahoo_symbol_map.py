"""
migrate_add_yahoo_symbol_map.py
=================================
Jednorazowa migracja: dodaje tabele yahoo_symbol_map (patrz models.py::
YahooSymbolMap) - cache symboli Yahoo Finance rozwiazywanych leniwie dla
tickerow T212 spoza automatycznego mapowania Finnhub (glownie gieldy poza
USA), patrz services/yahoo_resolver.py.

Bezpieczne do uruchomienia wielokrotnie (CREATE TABLE IF NOT EXISTS).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_yahoo_symbol_map.py
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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS yahoo_symbol_map (
            ticker VARCHAR(30) NOT NULL PRIMARY KEY,
            yahoo_symbol VARCHAR(30),
            resolved_at DATETIME NOT NULL
        )
    """)

    conn.commit()
    conn.close()
    print("OK - tabela yahoo_symbol_map istnieje.")


if __name__ == "__main__":
    main()
