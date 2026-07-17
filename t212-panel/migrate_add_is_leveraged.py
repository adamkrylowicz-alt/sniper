"""
migrate_add_is_leveraged.py
=============================
Jednorazowa migracja: dokłada kolumnę instruments.is_leveraged na istniejącej
bazie (instance/sniper.db) i OD RAZU przelicza ją dla WSZYSTKICH już
zsynchronizowanych instrumentów (nie trzeba czekać na kolejny refresh z T212 -
nazwy już są w lokalnym cache'u, to czysto lokalne przeliczenie).

Ten sam wzorzec wykrywania dźwigni co app/services/instrument_cache.py -
jeśli kiedyś zmienisz tam _LEVERAGE_PATTERN, zmień też tutaj (albo po prostu
odśwież cache z T212 - kolejne refresh_instrument_cache() i tak przeliczy
wszystko na nowo tym aktualnym wzorcem z instrument_cache.py).

Bezpieczne do uruchomienia wielokrotnie (idempotentne - sprawdza czy kolumna
już istnieje, a przeliczenie po prostu nadpisuje tym samym wynikiem).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_is_leveraged.py
"""

import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "sniper.db"

_LEVERAGE_PATTERN = re.compile(r"-?\d+x\b", re.IGNORECASE)


def main() -> None:
    if not DB_PATH.exists():
        print(f"Nie znaleziono bazy: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cols = [row[1] for row in cur.execute("PRAGMA table_info(instruments)")]
    if "is_leveraged" not in cols:
        cur.execute("ALTER TABLE instruments ADD COLUMN is_leveraged BOOLEAN DEFAULT 0")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_instruments_type_leveraged "
            "ON instruments (instrument_type, is_leveraged)"
        )
        conn.commit()
        print("Dodano kolumnę is_leveraged + indeks.")
    else:
        print("Kolumna is_leveraged już istnieje - przeliczam wartości od nowa.")

    rows = cur.execute("SELECT ticker, name, instrument_type FROM instruments").fetchall()
    updates = []
    for ticker, name, instrument_type in rows:
        is_leveraged = instrument_type == "ETF" and bool(_LEVERAGE_PATTERN.search(name or ""))
        updates.append((1 if is_leveraged else 0, ticker))

    cur.executemany("UPDATE instruments SET is_leveraged = ? WHERE ticker = ?", updates)
    conn.commit()

    leveraged_count = sum(1 for is_lev, _ in updates if is_lev)
    conn.close()
    print(f"OK - przeliczono {len(updates)} instrumentów, {leveraged_count} oznaczonych jako z dźwignią.")


if __name__ == "__main__":
    main()
