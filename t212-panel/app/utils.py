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


def ticker_display_name(ticker: str) -> str:
    """
    Nazwa spółki zamiast surowego tickera T212 (np. "V_US_EQ" -> "Visa") -
    patrz friendly_name() wyżej. Fallback na surowy ticker gdy instrument
    nie jest w lokalnym cache Instrument (nie powinno się zdarzyć dla
    własnej listy bota, ale bez zgadywania). Przeniesione z
    telegram_commands.py::_display_name 2026-08-07, żeby ten sam wzorzec
    dało się użyć też w alertach ERROR silników (bot/eod/signal _log()) -
    dotąd te alerty dalej waliły surowym tickerem na Telegramie mimo że
    /status już to rozwiązywał.
    """
    from .models import Instrument
    instrument = Instrument.query.get(ticker)
    if instrument is None or not instrument.name:
        return ticker
    return friendly_name(instrument.name) or ticker


_TICKER_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*):\s")


def humanize_ticker_prefix(message: str) -> str:
    """
    Podmienia WIODĄCY ticker w komunikacie (wzorzec "TICKER: reszta", np.
    "V_US_EQ: zakup nieudany...") na czytelną nazwę spółki - patrz
    ticker_display_name(). Bez dopasowania (nie zaczyna się od tickera, albo
    ticker nieznany w lokalnym cache) zwraca message bez zmian - celowo NIE
    zgaduje, żeby nie namieszać w komunikatach bez tickera (np.
    "Reconciliation: ...").
    """
    m = _TICKER_PREFIX_RE.match(message)
    if not m:
        return message
    ticker = m.group(1)
    name = ticker_display_name(ticker)
    if name == ticker:
        return message
    return name + message[len(ticker):]


def current_environment(user_id: int) -> str:
    """
    Zwraca "demo" albo "live" dla danego użytkownika - patrz
    models.py::UserSettings.active_environment (2026-08-06, zastąpiło stałe
    modułowe BOT_ENVIRONMENT/SIGNAL_ENVIRONMENT/EOD_ENVIRONMENT, zawsze
    "demo" na sztywno). Przyjmuje user_id WPROST (nie czyta flask.g) - musi
    działać też w tle w APScheduler jobach (tick()/reconcile()), które NIE
    mają kontekstu requestu. Brak wiersza UserSettings (nowy user) -> "demo",
    ten sam bezpieczny default co samo pole w modelu.
    """
    from .models import UserSettings

    settings = UserSettings.query.filter_by(user_id=user_id).first()
    return settings.active_environment if settings else "demo"


def telegram_env_tag(user_id: int) -> str:
    """
    Krótki, wizualnie mocny tag środowiska do KAŻDEJ wiadomości Telegram
    (Adam, 2026-08-07: "zrób coś żeby rozróżniać konto dev od real w
    telegramie"). Pierwotnie dev/prod miały dzielić jednego bota (@Snajper2026_
    bot) i ten sam chat_id, więc tag miał być JEDYNYM sposobem odróżnienia
    alertu w jednej rozmowie. Od 10.08.2026 dev/prod mają OSOBNE boty
    (@Snajperdev2026_bot / @Snajper2026_bot, potwierdzone Adamowi, świadomie
    zostaje tak - jasny fizyczny podział, zero ryzyka pomylenia alertu demo z
    realnymi pieniędzmi) - tag zostaje jako dodatkowe zabezpieczenie/
    czytelność w treści samej wiadomości, nie jedyny sposób rozróżnienia.
    Patrz current_environment() wyżej - "live" tu ZAWSZE znaczy realne
    pieniądze (patrz [[feedback_snajper_is_paper_trading_not_demo_flag]] w
    pamięci Claude - is_paper_trading to coś innego, nie mylić).
    """
    return "💰 LIVE" if current_environment(user_id) == "live" else "🧪 DEMO"


def avatar_hue(ticker: str) -> int:
    """
    Deterministyczny odcień (0-359) na podstawie tickera - ten sam ticker
    ZAWSZE dostaje ten sam kolor awatara. Współdzielone między
    routes/settings.py (watchlist) i routes/scalping.py (sidebar Warp Mode),
    żeby kolory się zgadzały w obu miejscach.
    """
    return sum(ord(c) for c in ticker) % 360
