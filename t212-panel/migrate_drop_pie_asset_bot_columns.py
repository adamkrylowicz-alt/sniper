"""
migrate_drop_pie_asset_bot_columns.py
=======================================
Jednorazowa migracja: usuwa z pie_assets trzy osierocone kolumny z odrzuconej
pierwszej wersji integracji bota - is_bot_allowed, is_penny_stock,
bot_entry_amount (patrz models.py::PieAsset - bot dostal wlasny, niezalezny
model BotAsset, PieAsset nigdy potem nie mial tych kolumn w ORM, ale zostaly
w bazie bo db.create_all() nie usuwa/zmienia istniejacych kolumn).

is_bot_allowed i is_penny_stock byly NOT NULL bez wartosci domyslnej -
KAZDY insert do pie_assets (czyli dodanie jakiegokolwiek aktywa do koszyka w
Smart Virtual Pie) konczyl sie 500 (IntegrityError), bo SQLAlchemy w ogole
nie zna tych kolumn i nigdy ich nie ustawia. Znalezione i zglosone przez
Adama 18.07.2026.

SQLite < 3.35 (m.in. NAS, ktory ma 3.34.1) NIE wspiera
"ALTER TABLE ... DROP COLUMN" - migracja robi wiec klasyczny rebuild tabeli
(nowa tabela wg aktualnego schema z models.py, kopia danych, podmiana nazw)
zamiast DROP COLUMN, zeby dzialac na obu wersjach SQLite.

UWAGA: stara migracja migrate_add_bot_entry_amount.py zostala USUNIETA -
dokladala dokladnie jedna z tych trzech osieroconych kolumn, wiec na SWIEZEJ
bazie (db.create_all() od zera, bez tych kolumn) odtwarzalaby ten sam bug.

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny juz nie ma).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_drop_pie_asset_bot_columns.py
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
    if "is_bot_allowed" not in cols:
        print("pie_assets nie ma juz osieroconych kolumn bota - nic do zrobienia.")
        conn.close()
        return

    cur.execute("PRAGMA foreign_keys=OFF")
    try:
        cur.execute("BEGIN TRANSACTION")
        cur.execute(
            """
            CREATE TABLE pie_assets_new (
                id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                pie_id INTEGER NOT NULL,
                ticker VARCHAR(30) NOT NULL,
                display_ticker VARCHAR(20) NOT NULL,
                currency VARCHAR(10) NOT NULL,
                target_weight NUMERIC(12, 4) NOT NULL,
                created_at DATETIME NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT uq_pie_ticker UNIQUE (pie_id, ticker),
                FOREIGN KEY(user_id) REFERENCES users (id),
                FOREIGN KEY(pie_id) REFERENCES pies (id)
            )
            """
        )
        cur.execute(
            """
            INSERT INTO pie_assets_new
                (id, user_id, pie_id, ticker, display_ticker, currency, target_weight, created_at)
            SELECT id, user_id, pie_id, ticker, display_ticker, currency, target_weight, created_at
            FROM pie_assets
            """
        )
        cur.execute("DROP TABLE pie_assets")
        cur.execute("ALTER TABLE pie_assets_new RENAME TO pie_assets")
        cur.execute("CREATE INDEX ix_pie_assets_pie_id ON pie_assets (pie_id)")
        cur.execute("CREATE INDEX ix_pie_assets_user_id ON pie_assets (user_id)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.execute("PRAGMA foreign_keys=ON")
        conn.close()

    print("OK - usunieto is_bot_allowed/is_penny_stock/bot_entry_amount z pie_assets (rebuild tabeli).")


if __name__ == "__main__":
    main()
