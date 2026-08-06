"""
migrate_add_environment_to_audit_logs.py
==========================================
Jednorazowa migracja: dokłada kolumnę `environment` (domyślnie "demo") do
trzech tabel dziennika (bot_audit_log/signal_audit_log/eod_audit_log) -
2026-08-07, ciąg dalszy tego samego dnia co
migrate_add_environment_to_bot_tables.py.

Powód: Adam po fixie tabel pozycji zauważył ten sam problem w Dzienniku -
"w dziennikach botow zostala historia na live" - stare wpisy z ery demo
(BUY/INFO/WARN/ERROR) dalej pokazywały się jako "aktualna" historia po
przełączeniu konta na live, bo dziennik nigdy nie filtrował po środowisku.

Default "demo" dla WSZYSTKICH istniejących wierszy jest poprawny historycznie -
przełącznik live nie istniał przed 2026-08-06.

Bezpieczne do uruchomienia wielokrotnie.

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_environment_to_audit_logs.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "sniper.db"

LOG_TABLES = ["bot_audit_log", "signal_audit_log", "eod_audit_log"]


def main() -> None:
    if not DB_PATH.exists():
        print(f"Nie znaleziono bazy: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for table in LOG_TABLES:
        cols = [row[1] for row in cur.execute(f"PRAGMA table_info({table})")]
        if "environment" in cols:
            print(f"{table}.environment już istnieje - pomijam.")
            continue
        cur.execute(f"ALTER TABLE {table} ADD COLUMN environment VARCHAR(10) NOT NULL DEFAULT 'demo'")
        print(f"OK - dodano {table}.environment (domyślnie 'demo').")

    conn.commit()
    conn.close()
    print("Migracja zakończona.")


if __name__ == "__main__":
    main()
