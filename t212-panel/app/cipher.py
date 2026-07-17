"""
cipher.py
=========
Moduł odpowiedzialny WYŁĄCZNIE za kryptografię w projekcie SNIPER.

Model bezpieczeństwa (Zero-Knowledge):
---------------------------------------
1. Dla każdego użytkownika generujemy jeden losowy MASTER KEY (32 bajty).
   To właśnie tym kluczem szyfrowane są realne sekrety (klucze API T212).

2. MASTER KEY nigdy nie jest zapisywany w bazie w formie jawnej.
   Zamiast tego zapisujemy go DWUKROTNIE zaszyfrowany (tzw. "key wrapping"):
     a) wrapped_by_password  -> odszyfrowywalny hasłem użytkownika
     b) wrapped_by_recovery  -> odszyfrowywalny kodem recovery

   Dzięki temu:
   - Admin nie zna ani hasła, ani recovery code -> nie ma dostępu do niczego.
   - Użytkownik, który zapomni hasła, może się dostać do środków przez
     recovery code, bez potrzeby resetu przez admina.

3. Klucze wyprowadzane z hasła/recovery code przez PBKDF2-HMAC-SHA256
   (parametr KDF_ITERATIONS - patrz niżej, uwaga do podbicia w configu).

Ten moduł NIE wie nic o modelach bazy danych ani o Flasku - jest czystą
biblioteką kryptograficzną, żeby dało się go łatwo testować w izolacji.
"""

from __future__ import annotations

import base64
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# ---------------------------------------------------------------------------
# UWAGA KONFIGURACYJNA:
# 100_000 iteracji to wartość "historyczna" z pierwotnego spec Gemini.
# Aktualna rekomendacja OWASP (2023+) dla PBKDF2-HMAC-SHA256 to ok. 600_000.
# Zostawiam to jako stałą do łatwej podmiany - podbicie iteracji nie psuje
# nic w istniejącej bazie, dopóki nie zaczniesz re-encryptować starych wpisów
# (stare wpisy trzeba by wtedy zmigrować, bo iteracje są zapisane per-rekord).
# ---------------------------------------------------------------------------
KDF_ITERATIONS = 600_000
SALT_BYTES = 16
RECOVERY_KEY_BYTES = 20  # ~32 znaki base32, wygodne do wpisania ręcznie


def generate_salt() -> bytes:
    """Losowa sól (unikalna per pole, per użytkownik)."""
    return os.urandom(SALT_BYTES)


def generate_master_key() -> bytes:
    """Losowy klucz Fernet (32 bajty przed base64), którym szyfrujemy sekrety."""
    return Fernet.generate_key()


def generate_recovery_code() -> str:
    """
    Generuje czytelny dla człowieka kod recovery, np. do wydrukowania/zapisania.
    Format: grupy po 5 znaków base32, oddzielone myślnikami.
    Przykład: 7XQK9-2MZP4-8VDRT-QAL3N
    """
    raw = secrets.token_bytes(RECOVERY_KEY_BYTES)
    b32 = base64.b32encode(raw).decode("utf-8").rstrip("=")
    groups = [b32[i:i + 5] for i in range(0, len(b32), 5)]
    return "-".join(groups)


def _derive_key(secret: str, salt: bytes, iterations: int = KDF_ITERATIONS) -> bytes:
    """
    Wyprowadza 32-bajtowy klucz z hasła/recovery-code + soli, zwraca go
    w formacie akceptowanym przez Fernet (urlsafe base64).
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    derived = kdf.derive(secret.encode("utf-8"))
    return base64.urlsafe_b64encode(derived)


def wrap_master_key(master_key: bytes, secret: str, salt: bytes) -> bytes:
    """
    Szyfruje master_key hasłem/recovery-code (key wrapping).
    Zwraca token Fernet gotowy do zapisania w bazie jako bytes.
    """
    wrapping_key = _derive_key(secret, salt)
    f = Fernet(wrapping_key)
    return f.encrypt(master_key)


def unwrap_master_key(wrapped: bytes, secret: str, salt: bytes) -> bytes:
    """
    Odszyfrowuje master_key przy pomocy hasła/recovery-code.
    Rzuca cryptography.fernet.InvalidToken jeśli hasło/kod jest błędny.
    """
    wrapping_key = _derive_key(secret, salt)
    f = Fernet(wrapping_key)
    return f.decrypt(wrapped)


def encrypt_secret(plaintext: str, master_key: bytes) -> bytes:
    """Szyfruje właściwy sekret (np. klucz API T212) master_key'em."""
    f = Fernet(master_key)
    return f.encrypt(plaintext.encode("utf-8"))


def decrypt_secret(token: bytes, master_key: bytes) -> str:
    """Odszyfrowuje sekret master_key'em. Rzuca InvalidToken przy błędzie."""
    f = Fernet(master_key)
    return f.decrypt(token).decode("utf-8")


class WrongCredentialsError(Exception):
    """Podniesione, gdy hasło lub recovery code nie pasują do zaszyfrowanych danych."""


def try_unwrap(wrapped: bytes, secret: str, salt: bytes) -> bytes:
    """
    Wygodny wrapper na unwrap_master_key, zamieniający InvalidToken
    na czytelny, dedykowany wyjątek domenowy.
    """
    try:
        return unwrap_master_key(wrapped, secret, salt)
    except InvalidToken as exc:
        raise WrongCredentialsError("Nieprawidłowe hasło lub kod recovery.") from exc


# ---------------------------------------------------------------------------
# Przykładowy pełny "flow" - do wykorzystania w warstwie serwisowej / testach:
#
#   salt_pw   = generate_salt()
#   salt_rec  = generate_salt()
#   recovery_code = generate_recovery_code()      # pokaż to userowi RAZ, do zapisania
#   master_key = generate_master_key()
#
#   wrapped_by_password = wrap_master_key(master_key, user_password, salt_pw)
#   wrapped_by_recovery = wrap_master_key(master_key, recovery_code, salt_rec)
#   # -> zapisujesz w bazie: wrapped_by_password, salt_pw, wrapped_by_recovery, salt_rec
#
#   # Logowanie hasłem:
#   master_key = try_unwrap(wrapped_by_password, user_password, salt_pw)
#
#   # Recovery (zapomniane hasło):
#   master_key = try_unwrap(wrapped_by_recovery, recovery_code, salt_rec)
#
#   # Szyfrowanie/odszyfrowanie właściwego klucza API T212:
#   token = encrypt_secret(t212_api_key, master_key)
#   t212_api_key = decrypt_secret(token, master_key)
# ---------------------------------------------------------------------------
