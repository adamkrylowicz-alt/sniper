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
from ..models import ApiKeySet
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
    return render_template("api_keys.html", has_demo=has_demo, has_live=has_live, error=None, saved=False)


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
