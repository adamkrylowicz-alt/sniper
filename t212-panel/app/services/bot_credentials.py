"""
app/services/bot_credentials.py
=================================
Poświadczenia (master_key) użytkowników z AKTYWNYM Micro-Grid Botem -
CELOWO osobny mechanizm od session_store.py (sesji przeglądarki), mimo że
strukturalnie identyczny (in-memory dict, jeden proces).

DLACZEGO OSOBNY OD session_store.py:
Sesja przeglądarki wygasa po 4h bezczynności (SESSION_TTL_SECONDS) - to
dobre dla logowania, złe dla bota, który ma działać 24/7 niezależnie od
tego czy masz otwartą przeglądarkę. Stąd bot dostaje WŁASNY, dłużej żyjący
(bez TTL - żyje dopóki nie wyłączysz bota albo nie zrestartujesz appki)
cache poświadczeń, aktywowany ŚWIADOMIE (ponowne podanie hasła w
routes/bot.py::activate), nie przy zwykłym logowaniu.

ZNANE OGRANICZENIE (ten sam kompromis co session_store.py):
Pamięć procesu = restart appki wyłącza WSZYSTKIE aktywne boty. To jest
WŁAŚCIWE zachowanie bezpieczeństwa, nie bug do naprawienia - po restarcie
nikt nie powinien mieć bota działającego "w ciemno" bez ponownego,
świadomego potwierdzenia hasłem. routes/bot.py pokazuje to wyraźnie w UI
(baza mówi is_bot_active=True, ale bez poświadczeń tutaj bot i tak nic
nie zrobi - patrz services/bot_engine.py::tick).
"""

from __future__ import annotations

_bot_credentials: dict[int, bytes] = {}  # {user_id: master_key}


def activate(user_id: int, master_key: bytes) -> None:
    _bot_credentials[user_id] = master_key


def get_master_key(user_id: int) -> bytes | None:
    return _bot_credentials.get(user_id)


def deactivate(user_id: int) -> None:
    _bot_credentials.pop(user_id, None)


def is_active(user_id: int) -> bool:
    return user_id in _bot_credentials


def active_user_ids() -> list[int]:
    """Do bot_engine.py::tick() - kogo w ogóle iterować przy każdym tick-u."""
    return list(_bot_credentials.keys())
