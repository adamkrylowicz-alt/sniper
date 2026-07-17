"""
migrate_add_pie_id.py
=======================
Jednorazowa migracja: dokłada kolumnę order_logs.pie_id na istniejącej bazie
(instance/sniper.db). db.create_all() (wołane automatycznie w app/__init__.py
przy każdym starcie appki) tworzy BRAKUJĄCE tabele (pies, pie_assets), ale
NIE dokłada nowych kolumn do już istniejących tabel - stąd ten osobny krok.

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumna już istnieje).

Uruchom RAZ, z katalogu t212-panel, PO tym jak appka choć raz wystartowała
z nowym models.py (żeby tabela `pies`, do której odwołuje się ten FK, już
istniała):

    python3 migrate_add_pie_id.py
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

    cols = [row[1] for row in cur.execute("PRAGMA table_info(order_logs)")]
    if "pie_id" in cols:
        print("pie_id już istnieje w order_logs - nic do zrobienia.")
        conn.close()
        return

    tables = [row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    if "pies" not in tables:
        print("Tabela 'pies' jeszcze nie istnieje - uruchom najpierw appkę raz "
              "(python3 run.py, potem Ctrl+C), żeby db.create_all() ją utworzyła.")
        conn.close()
        return

    cur.execute("ALTER TABLE order_logs ADD COLUMN pie_id INTEGER REFERENCES pies(id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_order_logs_pie_id ON order_logs (pie_id)")
    conn.commit()
    conn.close()
    print("OK - dodano order_logs.pie_id + indeks.")


if __name__ == "__main__":
    main()
