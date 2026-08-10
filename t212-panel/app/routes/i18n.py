"""
app/routes/i18n.py
===================
Przełącznik PL/EN (10.08.2026, Adam - appka ma być czytelna dla kogoś kto
nie zna polskiego). CELOWO bez @login_required - musi działać też na
ekranie logowania/rejestracji, zanim ktokolwiek się zaloguje.

Ciasteczko jest jedynym "od razu widocznym" stanem (wygrywa nad
UserSettings.language w context processorze - patrz __init__.py::
inject_auth_state) - dla zalogowanego usera dopisujemy wybór też do bazy,
żeby przetrwał zalogowanie na innej przeglądarce/urządzeniu.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, redirect, request, url_for

from ..extensions import db
from ..i18n import LANG_COOKIE_NAME, SUPPORTED_LANGUAGES
from ..utils import _load_session_into_g

i18n_bp = Blueprint("i18n", __name__)


@i18n_bp.route("/lang/<lang>", methods=["POST"])
def set_language(lang: str):
    if lang not in SUPPORTED_LANGUAGES:
        return jsonify(ok=False, error="unsupported language"), 400

    if _load_session_into_g():
        from flask import g
        from ..models import UserSettings
        settings = UserSettings.query.filter_by(user_id=g.user_id).first()
        if settings is None:
            settings = UserSettings(user_id=g.user_id)
            db.session.add(settings)
        settings.language = lang
        db.session.commit()

    next_path = request.form.get("next") or request.referrer or url_for("root")
    response = redirect(next_path)
    response.set_cookie(
        LANG_COOKIE_NAME,
        lang,
        httponly=False,  # JS (common.js) też czyta ten język dla t() po stronie klienta
        samesite="Lax",
        secure=request.is_secure,
        max_age=60 * 60 * 24 * 365,
    )
    return response
