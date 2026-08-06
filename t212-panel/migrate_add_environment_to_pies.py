"""
migrate_add_environment_to_pies.py
====================================
Jednorazowa migracja: dokłada kolumnę `pies.environment` (domyślnie "demo") -
2026-08-07, ciąg dalszy tego samego dnia co migrate_add_environment_to_bot_tables.py
i migrate_add_environment_to_audit_logs.py.

Powód: Adam - "virtual pie popraw bo wisza tematy z demo na live w nim" -
koszyki Smart Virtual Pie stworzone na koncie demo dalej pokazywały się na
liście po przełączeniu na live, bo `Pie` (jak wszystko inne w appce przed
dzisiejszym fixem) nigdy nie miało pojęcia o środowisku.

Default "demo" dla WSZYSTKICH istniejących wierszy jest poprawny historycznie.

Bezpieczne do uruchomienia wielokrotnie.

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_environment_to_pies.py
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

    cols = [row[1] for row in cur.execute("PRAGMA table_info(pies)")]
    if "environment" in cols:
        print("pies.environment już istnieje - nic do zrobienia.")
    else:
        cur.execute("ALTER TABLE pies ADD COLUMN environment VARCHAR(10) NOT NULL DEFAULT 'demo'")
        conn.commit()
        print("OK - dodano pies.environment (domyślnie 'demo').")

    conn.close()


if __name__ == "__main__":
    main()
