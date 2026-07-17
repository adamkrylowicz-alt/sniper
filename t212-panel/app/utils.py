"""
app/utils.py
============
Wspólne narzędzia do autoryzacji request'ów - login_required + dostęp do
danych zalogowanego użytkownika (g.user_id, g.master_key) w widokach.

Użycie w dowolnym blueprint:
    from ..utils import login_required, current_master_key

    @some_bp.route("/coś")
    @login_required
    def widok():
        master_key = current_master_key()
        ...
"""

from __future__ import annotations

import re
from functools import wraps

from flask import g, jsonify, redirect, request, url_for

from .services.session_store import SESSION_COOKIE_NAME, get_session


def _load_session_into_g() -> bool:
    """
    Wczytuje dane sesji (jeśli są) do flask.g dla bieżącego requestu.
    Zwraca True jeśli użytkownik jest zalogowany.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    data = get_session(token)
    if data is None:
        return False
    g.user_id = data.user_id
    g.master_key = data.master_key
    return True


def login_required(view_func):
    """
    Dekorator blokujący dostęp niezalogowanym.

    Zachowanie zależy od typu żądania:
    - JSON/API (np. POST /warp/order) -> zwraca 401 JSON, żeby JS w
      przeglądarce mógł to obsłużyć bez przeładowania strony
    - Zwykłe żądanie strony (GET) -> przekierowuje na /auth/login
    """
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not _load_session_into_g():
            if request.is_json or request.path.startswith("/warp/order"):
                return jsonify(ok=False, error="Musisz się zalogować.", auth_required=True), 401
            # next= tylko dla GET (nawigacja na stronę) - dla POST (np. przycisk
            # "Odśwież cache") request.path wskazuje endpoint przyjmujący
            # WYŁĄCZNIE POST, a po zalogowaniu next jest odwiedzane przez GET
            # (patrz auth.py::login_view) - dawało to 405 Method Not Allowed.
            if request.method == "GET":
                return redirect(url_for("auth.login_view", next=request.path))
            return redirect(url_for("auth.login_view"))
        return view_func(*args, **kwargs)
    return wrapped


def current_user_id() -> int:
    """Wygodny skrót - wywoływać TYLKO wewnątrz widoku chronionego @login_required."""
    return g.user_id


def current_master_key() -> bytes:
    """Wygodny skrót - wywoływać TYLKO wewnątrz widoku chronionego @login_required."""
    return g.master_key


_LEGAL_SUFFIX_RE = re.compile(
    r",?\s+(Incorporated|Inc|Corporation|Corp|Company|Co|Limited|Ltd|PLC|LLC|L\.P\.|LP|S\.A\.|SA|N\.V\.|NV|AG|ASA|Oyj|SE)\.?$",
    re.IGNORECASE,
)


def friendly_name(name: str | None) -> str | None:
    """
    Ucina formalne/prawne koncowki z nazwy instrumentu (np. "Apple Inc" ->
    "Apple") - T212 dostarcza formalne nazwy, appka ma pokazywac "potoczne".
    Celowo NIE tnie slow jak "Trust"/"Group"/"Holdings"/"ETF" - te czesto sa
    czescia rozpoznawalnej nazwy (np. ETF-y), nie tylko formalnoscia prawna.
    """
    if not name:
        return name
    return _LEGAL_SUFFIX_RE.sub("", name).strip()


def avatar_hue(ticker: str) -> int:
    """
    Deterministyczny odcień (0-359) na podstawie tickera - ten sam ticker
    ZAWSZE dostaje ten sam kolor awatara. Współdzielone między
    routes/settings.py (watchlist) i routes/scalping.py (sidebar Warp Mode),
    żeby kolory się zgadzały w obu miejscach.
    """
    return sum(ord(c) for c in ticker) % 360
