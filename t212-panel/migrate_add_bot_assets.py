"""
migrate_add_bot_assets.py
===========================
Jednorazowa migracja: tworzy tabelę bot_assets (własna, niezależna od Pie
lista tickerów bota - patrz models.py::BotAsset) i przepina
active_trades.pie_asset_id -> active_trades.bot_asset_id.

Bezpieczna do uruchomienia wielokrotnie (sprawdza czy tabela/kolumna już
istnieje). active_trades jest dziś zawsze puste (Etap 2 dopiero dochodzi do
strategii wejścia), więc RENAME COLUMN nie traci żadnych danych - ale
działa bezpiecznie nawet gdyby coś tam już było (SQLite RENAME COLUMN
zachowuje dane, tylko zmienia nazwę).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_bot_assets.py
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

    tables = [row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]

    if "bot_assets" not in tables:
        cur.execute("""
            CREATE TABLE bot_assets (
                id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                ticker VARCHAR(30) NOT NULL,
                display_ticker VARCHAR(20) NOT NULL,
                currency VARCHAR(10) NOT NULL,
                entry_amount NUMERIC(12, 2) NOT NULL,
                is_penny_stock BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT uq_bot_asset_user_ticker UNIQUE (user_id, ticker),
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
        """)
        cur.execute("CREATE INDEX ix_bot_assets_user_id ON bot_assets (user_id)")
        conn.commit()
        print("OK - utworzono tabelę bot_assets.")
    else:
        print("Tabela bot_assets już istnieje - pomijam tworzenie.")

    cols = [row[1] for row in cur.execute("PRAGMA table_info(active_trades)")]
    if "pie_asset_id" in cols and "bot_asset_id" not in cols:
        cur.execute("ALTER TABLE active_trades RENAME COLUMN pie_asset_id TO bot_asset_id")
        conn.commit()
        print("OK - active_trades.pie_asset_id -> bot_asset_id.")
    elif "bot_asset_id" in cols:
        print("active_trades.bot_asset_id już istnieje - pomijam zmianę nazwy.")
    else:
        print("UWAGA: active_trades nie ma ani pie_asset_id ani bot_asset_id - sprawdź ręcznie.")

    conn.close()


if __name__ == "__main__":
    main()
