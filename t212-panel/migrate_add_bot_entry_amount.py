"""
migrate_add_bot_entry_amount.py
=================================
Jednorazowa migracja: dokłada kolumnę pie_assets.bot_entry_amount na
istniejącej bazie (instance/sniper.db). Nullable, bez backfillu - NULL jest
poprawną wartością domyślną dla WSZYSTKICH istniejących wierszy (oznacza
"bot pomija ten ticker", patrz models.py::PieAsset.bot_entry_amount).

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumna już istnieje).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_bot_entry_amount.py
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

    cols = [row[1] for row in cur.execute("PRAGMA table_info(pie_assets)")]
    if "bot_entry_amount" in cols:
        print("bot_entry_amount już istnieje w pie_assets - nic do zrobienia.")
        conn.close()
        return

    cur.execute("ALTER TABLE pie_assets ADD COLUMN bot_entry_amount NUMERIC(12, 2)")
    conn.commit()
    conn.close()
    print("OK - dodano pie_assets.bot_entry_amount (NULL dla wszystkich istniejących wierszy).")


if __name__ == "__main__":
    main()
