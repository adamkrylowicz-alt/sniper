"""
migrate_add_market_data_keys.py
=================================
Jednorazowa migracja: tworzy tabelę market_data_key_sets (klucze do
Finnhub/Alpaca + opcjonalny adres własnej bramki IBKR, PER USER - patrz
models.py::MarketDataKeySet, 10.08.2026 Adam: "kazdy user ma miec swoje
klucze... nie moze korzystac z moich"). Jeden wiersz na usera.

Bezpieczna do uruchomienia wielokrotnie (sprawdza czy tabela już istnieje).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_market_data_keys.py
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

    if "market_data_key_sets" not in tables:
        cur.execute("""
            CREATE TABLE market_data_key_sets (
                id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                encrypted_finnhub_key BLOB,
                encrypted_alpaca BLOB,
                ibkr_host VARCHAR(255),
                ibkr_port INTEGER,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT uq_market_data_key_sets_user UNIQUE (user_id),
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
        """)
        conn.commit()
        print("OK - utworzono tabelę market_data_key_sets.")
    else:
        print("Tabela market_data_key_sets już istnieje - pomijam.")

    conn.close()


if __name__ == "__main__":
    main()
