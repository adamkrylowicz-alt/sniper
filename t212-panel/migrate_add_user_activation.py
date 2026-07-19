"""
migrate_add_user_activation.py
================================
Jednorazowa migracja: dokłada kolumny users.is_admin / email_verified /
is_active / activation_token (patrz models.py::User) - potrzebne odkąd
rejestracja jest publiczna (sniper.duckdns.org) i nowe konta wymagają
potwierdzenia mailem + ręcznego zatwierdzenia w panelu admina.

WAŻNE: istniejący user(rzy) w bazie (czyli Ty, zarejestrowany PRZED tą
migracją) dostają is_admin=1, email_verified=1, is_active=1 automatycznie -
inaczej ta migracja zablokowałaby Ci własne konto. Nowo rejestrowani od tej
pory startują z samymi zerami (patrz routes/auth.py::register_view).

Bezpieczne do uruchomienia wielokrotnie (sprawdza czy kolumny już istnieją).

Uruchom RAZ, z katalogu t212-panel:

    python3 migrate_add_user_activation.py
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

    cols = [row[1] for row in cur.execute("PRAGMA table_info(users)")]
    if "is_active" in cols:
        print("is_active już istnieje w users - nic do zrobienia.")
        conn.close()
        return

    cur.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    cur.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
    cur.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0")
    cur.execute("ALTER TABLE users ADD COLUMN activation_token VARCHAR(64)")
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_activation_token "
        "ON users(activation_token)"
    )

    # Backfill: konta istniejące PRZED ta migracja sa juz zaufane (zalozone
    # zanim rejestracja byla publiczna) - pierwsze z nich zostaje adminem.
    cur.execute(
        "UPDATE users SET is_admin = 1, email_verified = 1, is_active = 1 "
        "WHERE id = (SELECT MIN(id) FROM users)"
    )
    cur.execute(
        "UPDATE users SET email_verified = 1, is_active = 1 WHERE is_admin = 0"
    )

    conn.commit()
    conn.close()
    print(
        "OK - dodano users.is_admin/email_verified/is_active/activation_token. "
        "Istniejące konto(a) aktywowane, najstarsze ustawione jako admin."
    )


if __name__ == "__main__":
    main()
