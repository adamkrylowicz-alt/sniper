"""
config.py
=========
Konfiguracja Flaska. Wszystko czytane z .env (python-dotenv), żeby
sekrety (SECRET_KEY, ścieżka bazy) nigdy nie były zaszyte na sztywno
w kodzie ani nie trafiały do żadnego repo/gita.

Użycie w __init__.py:
    from .config import Config
    app.config.from_object(Config)
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# Wczytuje .env z katalogu głównego projektu (tam gdzie leży run.py).
load_dotenv()


def _str_to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    # Klucz do podpisywania sesji/ciasteczek Flaska. WYMAGANY w produkcji -
    # celowo brak wartości domyślnej "na sztywno", żeby appka nie wstała
    # cicho z niebezpiecznym, przewidywalnym kluczem.
    SECRET_KEY = os.environ.get("SECRET_KEY")

    # Ścieżka do pliku SQLite. UWAGA: Flask-SQLAlchemy SAM doprawia względne
    # ścieżki sqlite do app.instance_path - więc tu podajemy TYLKO nazwę
    # pliku, bez przedrostka "instance/", inaczej wychodzi podwójne
    # zagnieżdżenie (instance/instance/sniper.db) i błąd "unable to open
    # database file". Jeśli chcesz inną lokalizację, użyj ścieżki
    # BEZWZGLĘDNEJ (sqlite:////pełna/ścieżka/do/pliku.db - 4 slashe).
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///sniper.db"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Debug WYŁĄCZONY domyślnie - włączasz jawnie przez .env tylko lokalnie.
    # Patrz uwaga w run.py o ryzyku debug=True przy dostępie z zewnątrz.
    DEBUG = _str_to_bool(os.environ.get("FLASK_DEBUG"), default=False)

    # Środowisko T212 używane domyślnie, jeśli użytkownik nie ma jeszcze
    # zapisanego wyboru w UserSettings - "demo" jako bezpieczny default.
    T212_DEFAULT_ENV = os.environ.get("T212_DEFAULT_ENV", "demo")

    # Klucz do Finnhub (mini-wykresy w Smart Virtual Pie, patrz services/price_feed.py) -
    # źródło notowań NIEZALEŻNE od T212, żeby nie zjadać jego wąskiego rate limitu.
    # Etap 1: jeden globalny klucz (jak dawniej T212_DEMO_API_KEY_TEMP) - mechanizm
    # "klucz per użytkownik z fallbackiem na globalny" (PDF sekcja 6.1) to Etap 3.
    # Darmowe konto: https://finnhub.io/register
    FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

    # Alpaca Market Data API - główne źródło ceny "na żywo" dla tickerów
    # `*_US_EQ` w bocie (patrz services/price_feed.py::get_live_price),
    # Finnhub->Yahoo zostaje jako fallback. Dodane 2026-07-22 na życzenie
    # Adama (nowy dostawca danych dla rynków USA). Endpoint Market Data jest
    # wspólny dla kluczy paper/live - klucz zaczynający się na "PK" działa
    # tu tak samo jak klucz live.
    ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
    ALPACA_API_SECRET = os.environ.get("ALPACA_API_SECRET")

    # Klucz publikowalny (publishable) do Logo.dev - logotypy spółek w Watchlist
    # i Smart Virtual Pie (patrz services/logo_cache.py). Clearbit Logo API,
    # którego appka używała wcześniej, jest MARTWE (wygaszone przez HubSpot,
    # od grudnia 2025 nic nie zwraca) - stąd migracja. Darmowe konto (500k
    # zapytań/mies.): https://logo.dev - w Dashboard -> "Publishable Key".
    LOGO_DEV_API_KEY = os.environ.get("LOGO_DEV_API_KEY")

    # --- TYMCZASOWE, DO USUNIĘCIA po zbudowaniu auth.py ---------------------
    # Zanim istnieje logowanie użytkownika (i tym samym hasło do odszyfrowania
    # master_key przez cipher.py), testujemy scalping.py z kluczem demo
    # wczytanym wprost z .env. To NIE jest docelowy, bezpieczny flow -
    # to jest jawnie oznaczony skrót testowy. Gdy powstanie auth.py,
    # ten klucz i cała ta sekcja znikają, a klucz będzie brany z bazy
    # (odszyfrowany przez cipher.py hasłem zalogowanego użytkownika).
    T212_DEMO_API_KEY_TEMP = os.environ.get("T212_DEMO_API_KEY_TEMP")
    T212_DEMO_API_SECRET_TEMP = os.environ.get("T212_DEMO_API_SECRET_TEMP")
    # -------------------------------------------------------------------

    # --- Wysyłka zgłoszeń "Zgłoś problem" przez email (Gmail SMTP) ---------
    # Konfiguracja pod Gmail z App Password (Ustawienia Google -> Bezpieczeństwo
    # -> Weryfikacja dwuetapowa (musi być włączona) -> Hasła aplikacji ->
    # wygeneruj nowe dla "Mail" -> wklej 16-znakowy kod jako SMTP_PASSWORD.
    # Zwykłe hasło do konta Google TU NIE ZADZIAŁA.
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    SMTP_FROM = os.environ.get("SMTP_FROM") or SMTP_USER
    REPORT_TO_EMAIL = os.environ.get("REPORT_TO_EMAIL")
    # -------------------------------------------------------------------

    @classmethod
    def validate(cls) -> None:
        """
        Woła się to jawnie w create_app() - appka ma się wywalić GŁOŚNO
        przy starcie, jeśli brakuje SECRET_KEY, zamiast cicho wystartować
        z None i mieć niebezpieczne/losowe sesje.
        """
        if not cls.SECRET_KEY:
            raise RuntimeError(
                "Brak SECRET_KEY w .env! Wygeneruj np. przez:\n"
                "  python3 -c \"import secrets; print(secrets.token_hex(32))\"\n"
                "i wklej jako SECRET_KEY=... do pliku .env"
            )
