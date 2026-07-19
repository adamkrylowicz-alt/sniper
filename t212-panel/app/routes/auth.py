"""
app/routes/auth.py
===================
Rejestracja / logowanie / wylogowanie.

Flow rejestracji (Zero-Knowledge, patrz cipher.py):
1. Generujemy losowy master_key (nim będą szyfrowane klucze API T212).
2. Generujemy recovery_code (pokazujemy userowi RAZ, na ekranie po rejestracji).
3. "Zawijamy" (wrap) master_key hasłem usera ORAZ osobno recovery_code'em -
   dwie niezależne ścieżki odszyfrowania, zapisane w User.
4. password_hash (do samego LOGOWANIA) to osobny mechanizm (werkzeug) -
   nie ma nic wspólnego z kluczem szyfrującym.

Flow logowania:
1. Sprawdzamy password_hash (czy to na pewno ten user).
2. Jeśli tak - odszyfrowujemy master_key hasłem (cipher.try_unwrap).
   Jeśli hasło było poprawne dla logowania, MUSI też poprawnie odszyfrować
   master_key (to ta sama wartość hasła użyta w obu miejscach) - więc to
   się nie powinno nigdy rozjechać, ale i tak łapiemy WrongCredentialsError
   na wszelki wypadek (np. ręczna manipulacja bazą).
3. Tworzymy sesję server-side (session_store.create_session) i ustawiamy
   ciasteczko z tokenem.
"""

from __future__ import annotations

import re
import secrets
import time

from flask import Blueprint, current_app, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .. import cipher
from ..extensions import db
from ..models import User
from ..services import mailer
from ..services.session_store import (
    SESSION_COOKIE_NAME,
    create_session,
    destroy_session,
)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

MIN_PASSWORD_LENGTH = 10

# -- Throttling nieudanych logowan -------------------------------------------
# Prosty licznik w pamieci procesu (ten sam wzorzec co session_store.py/
# risk_guard.py - jeden proces, jeden user, restart czysci stan i to
# akceptowalne). Potrzebne odkad appka jest dostepna publicznie przez
# nginx-proxy-manager (sniper.duckdns.org), nie tylko w LAN - bez tego bot
# mogliby brute-forcowac haslo bez zadnego ograniczenia.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60  # tez czas "odblokowania" - najstarsza proba wypada z okna

_failed_logins: dict[str, list[float]] = {}


def _throttle_key(username: str) -> str:
    # IP + username (nie sam IP) - jeden zly aktor nie blokuje calej sieci
    # (np. NAT-owanej), a jeden username nie da sie zbrute-forcowac z wielu IP
    # bez proby na kazdy z osobna. request.remote_addr jest poprawny dzieki
    # ProxyFix (patrz __init__.py) nawet przez NPM.
    return f"{request.remote_addr}:{username.lower()}"


def _is_locked_out(key: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _failed_logins.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    _failed_logins[key] = recent
    return len(recent) >= LOGIN_MAX_ATTEMPTS


def _record_failed_login(key: str) -> None:
    _failed_logins.setdefault(key, []).append(time.monotonic())


def _clear_failed_logins(key: str) -> None:
    _failed_logins.pop(key, None)


# -- Throttling rejestracji ---------------------------------------------------
# Rejestracja jest teraz PUBLICZNA (patrz register_view - konto i tak startuje
# nieaktywne, wymaga maila + zatwierdzenia admina), ale bez limitu ktos moglby
# zasypac skrzynke Adama mailami/panel zatwierdzania smieciowymi kontami.
REGISTER_MAX_ATTEMPTS = 5
REGISTER_WINDOW_SECONDS = 60 * 60

_recent_registrations: dict[str, list[float]] = {}

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _is_registration_throttled(ip: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _recent_registrations.get(ip, []) if now - t < REGISTER_WINDOW_SECONDS]
    _recent_registrations[ip] = recent
    return len(recent) >= REGISTER_MAX_ATTEMPTS


def _record_registration(ip: str) -> None:
    _recent_registrations.setdefault(ip, []).append(time.monotonic())


def _send_mail_safe(*, mail_to: str, subject: str, body_text: str) -> None:
    """
    Wrapper na mailer.send_report - polyka bledy wysylki (brak SMTP w .env,
    Gmail odmowil itp.) zamiast wywalac 500 przy rejestracji. Rejestracja
    ma sie udac NIEZALEZNIE od tego czy mail dojdzie - konto i tak czeka
    na zatwierdzenie admina, ktory moze je zatwierdzic recznie nawet bez
    linku aktywacyjnego jesli mail nie dotarl.
    """
    cfg = current_app.config
    try:
        mailer.send_report(
            smtp_host=cfg.get("SMTP_HOST"),
            smtp_port=cfg.get("SMTP_PORT"),
            smtp_user=cfg.get("SMTP_USER"),
            smtp_password=cfg.get("SMTP_PASSWORD"),
            mail_from=cfg.get("SMTP_FROM"),
            mail_to=mail_to,
            subject=subject,
            body_text=body_text,
        )
    except (mailer.MailerNotConfiguredError, mailer.MailSendError) as exc:
        current_app.logger.warning("Nie udalo sie wyslac maila do %s: %s", mail_to, exc)


def _password_policy_error(username: str, password: str) -> str | None:
    """
    Prosta walidacja siły hasła. Świadomie NIE używam biblioteki zxcvbn
    (z pierwotnego spec Gemini) - na tej wersji Pythona/NAS-a (3.8, ręczna
    instalacja pip przez --user) to dodatkowa zależność do zepsucia się
    przy instalacji, a korzyść przy jednoosobowym użyciu jest marginalna.
    Jeśli chcesz mocniejszą walidację, to jest jedyne miejsce do zmiany.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Hasło musi mieć minimum {MIN_PASSWORD_LENGTH} znaków."
    if password.lower() == username.lower():
        return "Hasło nie może być takie samo jak nazwa użytkownika."
    return None


@auth_bp.route("/register", methods=["GET", "POST"])
def register_view():
    """
    Rejestracja jest publiczna (appka od 19.07.2026 dostepna przez
    nginx-proxy-manager, sniper.duckdns.org), ale konto NIE moze sie
    zalogowac dopoki nie przejdzie dwoch niezaleznych bramek:
      1. email_verified - klik w link aktywacyjny wyslany na username
         (traktowany jako adres email - patrz EMAIL_PATTERN).
      2. is_active - reczne zatwierdzenie przez admina w panelu
         (Ustawienia -> Oczekujace konta, patrz routes/settings.py).
    Pierwszy user w bazie (Ty) zostaje adminem automatycznie.
    """
    if request.method == "GET":
        return render_template("auth/register.html", error=None)

    if _is_registration_throttled(request.remote_addr):
        return render_template(
            "auth/register.html",
            error="Zbyt wiele rejestracji z tego adresu. Spróbuj później.",
        )

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    password_confirm = request.form.get("password_confirm") or ""

    if not username or not password:
        return render_template("auth/register.html", error="Wypełnij wszystkie pola.")

    if not EMAIL_PATTERN.match(username):
        return render_template(
            "auth/register.html",
            error="Podaj prawidłowy adres e-mail jako nazwę użytkownika - trafi tam link aktywacyjny.",
        )

    if password != password_confirm:
        return render_template("auth/register.html", error="Hasła nie są identyczne.")

    policy_error = _password_policy_error(username, password)
    if policy_error:
        return render_template("auth/register.html", error=policy_error)

    if User.query.filter_by(username=username).first() is not None:
        return render_template("auth/register.html", error="Ta nazwa użytkownika jest już zajęta.")

    _record_registration(request.remote_addr)

    # -- Zero-Knowledge key wrapping ------------------------------------
    master_key = cipher.generate_master_key()
    recovery_code = cipher.generate_recovery_code()

    salt_password = cipher.generate_salt()
    salt_recovery = cipher.generate_salt()

    wrapped_by_password = cipher.wrap_master_key(master_key, password, salt_password)
    wrapped_by_recovery = cipher.wrap_master_key(master_key, recovery_code, salt_recovery)

    is_first_user = User.query.count() == 0
    user = User(
        username=username,
        password_hash=generate_password_hash(password),
        wrapped_master_key_by_password=wrapped_by_password,
        salt_password=salt_password,
        wrapped_master_key_by_recovery=wrapped_by_recovery,
        salt_recovery=salt_recovery,
        is_admin=is_first_user,
        email_verified=is_first_user,
        is_active=is_first_user,
        activation_token=None if is_first_user else secrets.token_urlsafe(32),
    )
    db.session.add(user)
    db.session.commit()

    if not is_first_user:
        activation_url = url_for("auth.activate_view", token=user.activation_token, _external=True)
        _send_mail_safe(
            mail_to=username,
            subject="SNIPER - potwierdź rejestrację",
            body_text=(
                f"Kliknij link, żeby potwierdzić adres e-mail:\n{activation_url}\n\n"
                "Konto dodatkowo wymaga ręcznego zatwierdzenia przez administratora - "
                "dopiero wtedy będziesz mógł się zalogować."
            ),
        )
        admin_email = current_app.config.get("REPORT_TO_EMAIL")
        if admin_email:
            _send_mail_safe(
                mail_to=admin_email,
                subject="SNIPER - nowa rejestracja czeka na zatwierdzenie",
                body_text=(
                    f"Nowe konto: {username}\n"
                    "Zatwierdź albo odrzuć w Ustawienia -> Oczekujące konta."
                ),
            )

    # Recovery code pokazujemy TERAZ, JEDEN JEDYNY RAZ - renderujemy go
    # bezpośrednio w odpowiedzi, nie zapisujemy nigdzie jawnie, nie
    # przekierowujemy (żeby nie zgubić go w query stringu/logach).
    return render_template(
        "auth/recovery_code.html",
        username=username,
        recovery_code=recovery_code,
        pending_activation=not is_first_user,
    )


@auth_bp.route("/activate/<token>", methods=["GET"])
def activate_view(token):
    """
    Klik w link z maila - potwierdza tylko, ze adres jest prawdziwy
    (email_verified=True). NIE aktywuje logowania - to osobny krok
    (is_active), reczny, w panelu admina.
    """
    user = User.query.filter_by(activation_token=token).first()
    if user is None:
        return render_template(
            "auth/activate_result.html", ok=False,
            message="Link jest nieprawidłowy albo już użyty.",
        )

    if not user.email_verified:
        user.email_verified = True
        db.session.commit()

    message = "Adres e-mail potwierdzony. " + (
        "Konto jest już zatwierdzone, możesz się zalogować."
        if user.is_active else
        "Teraz poczekaj, aż administrator ręcznie zatwierdzi konto."
    )
    return render_template("auth/activate_result.html", ok=True, message=message)


@auth_bp.route("/login", methods=["GET", "POST"])
def login_view():
    if request.method == "GET":
        return render_template("auth/login.html", error=None, next=request.args.get("next"))

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_path = request.form.get("next") or url_for("scalping.warp_view")

    throttle_key = _throttle_key(username)
    generic_error = "Nieprawidłowa nazwa użytkownika lub hasło."

    if _is_locked_out(throttle_key):
        return render_template(
            "auth/login.html",
            error="Zbyt wiele nieudanych prób logowania. Spróbuj ponownie za kilkanaście minut.",
            next=next_path,
        )

    user = User.query.filter_by(username=username).first()

    if user is None or not check_password_hash(user.password_hash, password):
        # Celowo IDENTYCZNY komunikat dla "brak usera" i "złe hasło" -
        # nie zdradzamy atakującemu, czy dana nazwa użytkownika istnieje.
        _record_failed_login(throttle_key)
        return render_template("auth/login.html", error=generic_error, next=next_path)

    _clear_failed_logins(throttle_key)

    if not user.is_active:
        pending_msg = (
            "Potwierdź adres e-mail klikając w link wysłany podczas rejestracji."
            if not user.email_verified else
            "Konto czeka na ręczne zatwierdzenie przez administratora."
        )
        return render_template("auth/login.html", error=pending_msg, next=next_path)

    try:
        master_key = cipher.try_unwrap(
            user.wrapped_master_key_by_password, password, user.salt_password
        )
    except cipher.WrongCredentialsError:
        # Teoretycznie nie powinno się zdarzyć skoro password_hash się zgodził -
        # ale łapiemy jawnie zamiast dać 500, gdyby ktoś kiedyś ręcznie
        # majstrował w bazie i rozjechał sobie dane.
        return render_template(
            "auth/login.html",
            error="Błąd odszyfrowania klucza - skontaktuj się z adminem.",
            next=next_path,
        )

    remember_me = request.form.get("remember_me") == "on"
    # 4h domyślnie, 30 dni jeśli user zaznaczy "zapamiętaj mnie". Kompromis
    # bezpieczeństwo/wygoda - to appka do TRADINGU na własnym prywatnym NAS-ie,
    # nie bank, więc 30 dni jest tu rozsądne. Sesja i tak żyje tylko w pamięci
    # serwera (session_store.py) - restart appki wyloguje niezależnie od tego.
    ttl_seconds = 60 * 60 * 24 * 30 if remember_me else 60 * 60 * 4

    token = create_session(user.id, master_key, ttl_seconds=ttl_seconds)

    response = redirect(next_path)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="Lax",
        # Dynamiczne, nie na sztywno - ta sama appka jest dostępna i po
        # zwykłym http://192.168.1.50:5050 w LAN-ie (gdzie secure=True
        # zablokowałoby zapisanie ciasteczka w ogóle), i po https:// przez
        # nginx-proxy-manager na zewnątrz. request.is_secure poprawnie
        # rozróżnia oba przypadki dzięki ProxyFix (patrz __init__.py),
        # który czyta nagłówek X-Forwarded-Proto ustawiany przez NPM.
        secure=request.is_secure,
        max_age=ttl_seconds,
    )
    return response


@auth_bp.route("/logout", methods=["POST"])
def logout_view():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    destroy_session(token)
    response = redirect(url_for("auth.login_view"))
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@auth_bp.route("/recover", methods=["GET", "POST"])
def recover_view():
    """
    Odzyskanie dostępu przez recovery_code (dla kogoś kto zapomniał hasła).

    Flow:
    1. User podaje username + recovery_code + nowe hasło.
    2. Próbujemy odszyfrować master_key kodem recovery (cipher.try_unwrap).
       Jeśli się nie uda - kod był zły (albo user nie istnieje) - generyczny
       błąd, bez zdradzania które z dwóch.
    3. Jeśli się uda - "przewijamy" master_key NOWYM hasłem (nowa sól),
       ORAZ generujemy NOWY recovery_code (stary uznajemy za zużyty/ujawniony,
       bo user właśnie go wpisał do formularza - dobra praktyka to go
       zrotować, żeby nie został ten sam kod używany w nieskończoność).
    4. Pokazujemy NOWY recovery_code (ten sam ekran co przy rejestracji).
    """
    if request.method == "GET":
        return render_template("auth/recover.html", error=None)

    username = (request.form.get("username") or "").strip()
    recovery_code = (request.form.get("recovery_code") or "").strip()
    new_password = request.form.get("new_password") or ""
    new_password_confirm = request.form.get("new_password_confirm") or ""

    generic_error = "Nieprawidłowa nazwa użytkownika lub kod odzyskiwania."

    user = User.query.filter_by(username=username).first()
    if user is None:
        return render_template("auth/recover.html", error=generic_error)

    try:
        master_key = cipher.try_unwrap(
            user.wrapped_master_key_by_recovery, recovery_code, user.salt_recovery
        )
    except cipher.WrongCredentialsError:
        return render_template("auth/recover.html", error=generic_error)

    if new_password != new_password_confirm:
        return render_template("auth/recover.html", error="Nowe hasła nie są identyczne.")

    policy_error = _password_policy_error(username, new_password)
    if policy_error:
        return render_template("auth/recover.html", error=policy_error)

    # Nowe hasło -> nowa sól, nowy wrap. Stary wrapped_by_password staje się
    # bezużyteczny (nikt go już nie odszyfruje starym hasłem - to zamierzone,
    # stare hasło przestaje działać).
    new_salt_password = cipher.generate_salt()
    user.wrapped_master_key_by_password = cipher.wrap_master_key(
        master_key, new_password, new_salt_password
    )
    user.salt_password = new_salt_password
    user.password_hash = generate_password_hash(new_password)

    # Rotacja recovery_code - stary uznajemy za potencjalnie ujawniony.
    new_recovery_code = cipher.generate_recovery_code()
    new_salt_recovery = cipher.generate_salt()
    user.wrapped_master_key_by_recovery = cipher.wrap_master_key(
        master_key, new_recovery_code, new_salt_recovery
    )
    user.salt_recovery = new_salt_recovery

    db.session.commit()

    return render_template(
        "auth/recovery_code.html",
        username=username,
        recovery_code=new_recovery_code,
        is_rotation=True,
    )
