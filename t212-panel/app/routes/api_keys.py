"""
app/routes/api_keys.py
========================
Widok do zapisania własnego klucza+sekretu T212 - zaszyfrowanego master_key
zalogowanego użytkownika (z sesji, patrz session_store.py), zamiast
tymczasowego rozwiązania z .env (T212_DEMO_API_KEY_TEMP), które było
tylko prowizorką na czas testów przed zbudowaniem auth.py.
"""

from __future__ import annotations

import json

from flask import Blueprint, redirect, render_template, request, url_for

from .. import cipher
from ..extensions import db
from ..models import ApiKeySet, MarketDataKeySet
from ..utils import current_master_key, current_user_id, login_required

api_keys_bp = Blueprint("api_keys", __name__, url_prefix="/settings/api-keys")


def get_decrypted_credentials(user_id: int, master_key: bytes, environment: str) -> dict | None:
    """
    Wygodna funkcja pomocnicza do użycia przez INNE blueprinty (np.
    routes/scalping.py), żeby nie duplikować logiki odszyfrowania.
    Zwraca {"api_key": ..., "api_secret": ...} albo None jeśli user
    nie ma jeszcze zapisanego klucza dla danego środowiska.
    """
    entry = ApiKeySet.query.filter_by(user_id=user_id, environment=environment).first()
    if entry is None:
        return None
    plaintext = cipher.decrypt_secret(entry.encrypted_key, master_key)
    return json.loads(plaintext)


@api_keys_bp.route("/", methods=["GET"])
@login_required
def view():
    user_id = current_user_id()
    has_demo = ApiKeySet.query.filter_by(user_id=user_id, environment="demo").first() is not None
    has_live = ApiKeySet.query.filter_by(user_id=user_id, environment="live").first() is not None
    market_keys = MarketDataKeySet.query.filter_by(user_id=user_id).first()
    return render_template(
        "api_keys.html", has_demo=has_demo, has_live=has_live, error=None, saved=False,
        has_finnhub=bool(market_keys and market_keys.encrypted_finnhub_key),
        has_alpaca=bool(market_keys and market_keys.encrypted_alpaca),
        ibkr_host=market_keys.ibkr_host if market_keys else "",
        ibkr_port=market_keys.ibkr_port if market_keys else "",
    )


@api_keys_bp.route("/save", methods=["POST"])
@login_required
def save():
    user_id = current_user_id()
    master_key = current_master_key()

    environment = request.form.get("environment")
    api_key = (request.form.get("api_key") or "").strip()
    api_secret = (request.form.get("api_secret") or "").strip()

    has_demo = ApiKeySet.query.filter_by(user_id=user_id, environment="demo").first() is not None
    has_live = ApiKeySet.query.filter_by(user_id=user_id, environment="live").first() is not None

    if environment not in ("demo", "live") or not api_key or not api_secret:
        return render_template(
            "api_keys.html", has_demo=has_demo, has_live=has_live,
            error="Wypełnij wszystkie pola.", saved=False,
        )

    payload = json.dumps({"api_key": api_key, "api_secret": api_secret})
    encrypted = cipher.encrypt_secret(payload, master_key)

    existing = ApiKeySet.query.filter_by(user_id=user_id, environment=environment).first()
    if existing:
        existing.encrypted_key = encrypted
    else:
        db.session.add(ApiKeySet(user_id=user_id, environment=environment, encrypted_key=encrypted))
    db.session.commit()

    return redirect(url_for("api_keys.view"))


@api_keys_bp.route("/delete", methods=["POST"])
@login_required
def delete():
    """
    Kasuje zapisany klucz dla danego środowiska (demo/live) - Adam,
    2026-08-06, przy okazji migracji prod/dev na osobne konta T212 ("dodaj
    opcje kasowania kluczy api do trading212 demo i real"). Samo skasowanie
    NIE dotyka is_bot_active/is_active w ustawieniach silników - jeśli bot
    był aktywny z tym kluczem, poświadczenia w pamięci (bot_credentials)
    zostają aż do restartu appki/deaktywacji, ale kolejny reconcile()/tick()
    wymagający ŚWIEŻEGO odczytu klucza (np. po restarcie) po prostu nic nie
    znajdzie - to ten sam, już istniejący fail-safe co brak klucza od zawsze.
    """
    user_id = current_user_id()
    environment = request.form.get("environment")
    if environment in ("demo", "live"):
        ApiKeySet.query.filter_by(user_id=user_id, environment=environment).delete()
        db.session.commit()
    return redirect(url_for("api_keys.view"))


def _get_or_create_market_keys(user_id: int) -> MarketDataKeySet:
    entry = MarketDataKeySet.query.filter_by(user_id=user_id).first()
    if entry is None:
        entry = MarketDataKeySet(user_id=user_id)
        db.session.add(entry)
    return entry


@api_keys_bp.route("/market-data/save", methods=["POST"])
@login_required
def save_market_data():
    """
    Zapisuje WŁASNE klucze usera do źródeł danych rynkowych (Finnhub/
    Alpaca) + opcjonalny adres własnej bramki IBKR (10.08.2026) - JEDEN
    formularz, TRZY niezależne sekcje, każda zapisywana TYLKO jeśli
    faktycznie wypełniona (puste pole = bez zmian w tej sekcji, nie
    kasowanie - do tego jest osobny przycisk "Usuń" per sekcja, patrz
    delete_market_data).
    """
    user_id = current_user_id()
    master_key = current_master_key()

    finnhub_key = (request.form.get("finnhub_key") or "").strip()
    alpaca_key = (request.form.get("alpaca_key") or "").strip()
    alpaca_secret = (request.form.get("alpaca_secret") or "").strip()
    ibkr_host = (request.form.get("ibkr_host") or "").strip()
    ibkr_port = (request.form.get("ibkr_port") or "").strip()

    entry = None

    if finnhub_key:
        entry = _get_or_create_market_keys(user_id)
        entry.encrypted_finnhub_key = cipher.encrypt_secret(finnhub_key, master_key)

    if alpaca_key or alpaca_secret:
        if not (alpaca_key and alpaca_secret):
            db.session.rollback()
            has_demo = ApiKeySet.query.filter_by(user_id=user_id, environment="demo").first() is not None
            has_live = ApiKeySet.query.filter_by(user_id=user_id, environment="live").first() is not None
            market_keys = MarketDataKeySet.query.filter_by(user_id=user_id).first()
            return render_template(
                "api_keys.html", has_demo=has_demo, has_live=has_live, saved=False,
                error="Podaj Alpaca key i secret razem, oba pola.",
                has_finnhub=bool(market_keys and market_keys.encrypted_finnhub_key),
                has_alpaca=bool(market_keys and market_keys.encrypted_alpaca),
                ibkr_host=market_keys.ibkr_host if market_keys else "",
                ibkr_port=market_keys.ibkr_port if market_keys else "",
            )
        entry = entry or _get_or_create_market_keys(user_id)
        payload = json.dumps({"api_key": alpaca_key, "api_secret": alpaca_secret})
        entry.encrypted_alpaca = cipher.encrypt_secret(payload, master_key)

    if ibkr_host or ibkr_port:
        entry = entry or _get_or_create_market_keys(user_id)
        entry.ibkr_host = ibkr_host or None
        entry.ibkr_port = int(ibkr_port) if ibkr_port.isdigit() else None

    db.session.commit()
    return redirect(url_for("api_keys.view"))


@api_keys_bp.route("/market-data/delete", methods=["POST"])
@login_required
def delete_market_data():
    """Czyści JEDNĄ sekcję (finnhub/alpaca/ibkr) - wiersz zostaje, tylko te pola wracają do NULL."""
    user_id = current_user_id()
    field = request.form.get("field")
    entry = MarketDataKeySet.query.filter_by(user_id=user_id).first()
    if entry is not None and field in ("finnhub", "alpaca", "ibkr"):
        if field == "finnhub":
            entry.encrypted_finnhub_key = None
        elif field == "alpaca":
            entry.encrypted_alpaca = None
        elif field == "ibkr":
            entry.ibkr_host = None
            entry.ibkr_port = None
        db.session.commit()
    return redirect(url_for("api_keys.view"))
