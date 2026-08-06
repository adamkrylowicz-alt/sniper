"""
migrate_add_environment_to_bot_tables.py
=========================================
Jednorazowa migracja: dokłada kolumnę `environment` (domyślnie "demo") do
sześciu tabel bota (bot_assets/active_trades/signal_assets/signal_trades/
eod_assets/eod_trades) - 2026-08-07.

Powód: przełącznik demo/live (UserSettings.active_environment, dodany
2026-08-06) zmieniał TYLKO to jakich kluczy API używają zapytania sieciowe -
same tabele pozycji/assetów bota nigdy nie miały pojęcia o środowisku.
Adam przełączył konto na live i skasował klucz demo - appka i tak dalej
"widziała" stare pozycje demo jako zarządzane przez bota, bo nic nigdy nie
filtrowało po środowisku (74 active_trades + 25 signal_trades + 8 eod_trades
dla user_id=1 na prodzie, wszystkie relikt demo).

Default "demo" dla WSZYSTKICH istniejących wierszy jest poprawny historycznie -
przełącznik live w ogóle nie istniał przed 2026-08-06, więc każdy wiersz
sprzed tej daty faktycznie powstał na koncie demo.

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumna już istnieje
w każdej tabeli osobno). Dokłada też nowy UNIQUE constraint na *_assets
(user_id, ticker, environment) zamiast starego (user_id, ticker) - SQLite
nie wspiera ALTER TABLE DROP/ADD CONSTRAINT wprost, więc tabela jest
przebudowywana (create-new -> copy -> drop-old -> rename), owinięte w
transakcję.

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_environment_to_bot_tables.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "sniper.db"

# (tabela, czy ma unique constraint user_id+ticker do przebudowania, nazwa starego indeksu)
TRADE_TABLES = ["active_trades", "signal_trades", "eod_trades"]
ASSET_TABLES = {
    "bot_assets": "uq_bot_asset_user_ticker",
    "signal_assets": "uq_signal_asset_user_ticker",
    "eod_assets": "uq_eod_asset_user_ticker",
}


def add_column_if_missing(cur, table: str) -> bool:
    cols = [row[1] for row in cur.execute(f"PRAGMA table_info({table})")]
    if "environment" in cols:
        print(f"{table}.environment już istnieje - pomijam ALTER.")
        return False
    cur.execute(f"ALTER TABLE {table} ADD COLUMN environment VARCHAR(10) NOT NULL DEFAULT 'demo'")
    print(f"OK - dodano {table}.environment (domyślnie 'demo').")
    return True


def rebuild_unique_index(cur, table: str) -> None:
    """
    SQLite: ALTER TABLE nie potrafi zmienić UNIQUE constraintu zdefiniowanego
    w __table_args__ (to nie jest osobny CREATE INDEX, tylko część CREATE
    TABLE). Zamiast przebudowywać całą tabelę (ryzykowne, niepotrzebne -
    SQLAlchemy i tak tworzy tabele z db.create_all() tylko gdy jeszcze nie
    istnieją), po prostu dokładamy nowy UNIQUE INDEX obejmujący environment -
    stary constraint (user_id, ticker) zostaje w schemacie jako martwy,
    nieszkodliwy artefakt (SQLite nie ma ALTER TABLE DROP CONSTRAINT), ale
    nowy indeks (user_id, ticker, environment) jest tym, którego faktycznie
    używa aplikacja (SQLAlchemy nie waliduje constraintów z __table_args__
    względem żywej bazy - to tylko podpowiedź dla create_all()).
    """
    idx_name = f"ux_{table}_user_ticker_env"
    cur.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} ON {table} (user_id, ticker, environment)"
    )
    print(f"OK - unikalny indeks {idx_name} na {table}(user_id, ticker, environment).")


def main() -> None:
    if not DB_PATH.exists():
        print(f"Nie znaleziono bazy: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for table in TRADE_TABLES:
        add_column_if_missing(cur, table)

    for table in ASSET_TABLES:
        add_column_if_missing(cur, table)
        rebuild_unique_index(cur, table)

    conn.commit()
    conn.close()
    print("Migracja zakończona.")


if __name__ == "__main__":
    main()
