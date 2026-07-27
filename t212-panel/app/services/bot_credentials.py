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

ZMIANA 2026-07-27 (autostart PO RESTARCIE, TYMCZASOWE "DO ODWOŁANIA"):
Adam poprosił wprost - dopóki appka nie jest udostępniona nikomu innemu
(single-user, tylko on), restart appki (a w tej sesji zdarzał się często
przy każdym hotfixie) nie powinien wymagać ręcznego ponownego wpisania
hasła w przeglądarce za każdym razem. ŚWIADOMY, WYRAŹNIE PROSZONY kompromis
bezpieczeństwa - master_key jest teraz PRZY AKTYWACJI zapisywany też do
pliku `instance/bot_autostart_keys.json` (chmod 600, katalog `instance/`
jest CAŁKOWICIE w .gitignore, nigdy nie trafi do repo), a `load_autostart()`
(wołane raz przy starcie appki z app/__init__.py::_register_scheduler,
TYLKO w prawdziwym procesie serwującym - patrz tam) odtwarza z niego
`_bot_credentials` bez pytania o hasło. Przy jawnym `deactivate()` wpis
usuwany z pliku - świadome wyłączenie zostaje wyłączone i po restarcie.
DO ODWOŁANIA: gdy appka będzie udostępniona komukolwiek innemu, Adam ma
to cofnąć (przestać wołać load_autostart() przy starcie, ewentualnie
skasować już zapisany plik) - wraca wtedy stare zachowanie z punktu niżej.

STARE, WŁAŚCIWE (docelowe) zachowanie bez autostartu:
Pamięć procesu = restart appki wyłącza WSZYSTKIE aktywne boty, wymagając
ponownego, świadomego potwierdzenia hasłem - nikt nie powinien mieć bota
działającego "w ciemno" bez tego, zwłaszcza gdy appka jest dostępna dla
kogoś więcej niż tylko Adama. routes/bot.py pokazuje to wyraźnie w UI
(baza mówi is_bot_active=True, ale bez poświadczeń tutaj bot i tak nic
nie zrobi - patrz services/bot_engine.py::tick).
"""

from __future__ import annotations

import base64
import json
import os

_bot_credentials: dict[int, bytes] = {}  # {user_id: master_key}

_AUTOSTART_FILENAME = "bot_autostart_keys.json"


def activate(user_id: int, master_key: bytes, instance_path: str | None = None) -> None:
    _bot_credentials[user_id] = master_key
    if instance_path is not None:
        _persist_autostart(instance_path)


def get_master_key(user_id: int) -> bytes | None:
    return _bot_credentials.get(user_id)


def deactivate(user_id: int, instance_path: str | None = None) -> None:
    _bot_credentials.pop(user_id, None)
    if instance_path is not None:
        _persist_autostart(instance_path)


def is_active(user_id: int) -> bool:
    return user_id in _bot_credentials


def active_user_ids() -> list[int]:
    """Do bot_engine.py::tick() - kogo w ogóle iterować przy każdym tick-u."""
    return list(_bot_credentials.keys())


def _persist_autostart(instance_path: str) -> None:
    """
    Zapisuje CAŁY aktualny stan `_bot_credentials` do pliku (nadpisuje, nie
    dopisuje) - wołane po KAŻDEJ zmianie (activate/deactivate), żeby plik
    zawsze odzwierciedlał to co realnie jest w pamięci. Zapis przez plik
    tymczasowy + os.replace (atomowe podmienienie) - restart appki w
    trakcie zapisu nie może zostawić uszkodzonego/pustego pliku.
    """
    path = os.path.join(instance_path, _AUTOSTART_FILENAME)
    data = {str(uid): base64.b64encode(key).decode("ascii") for uid, key in _bot_credentials.items()}
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)


def load_autostart(instance_path: str) -> list[int]:
    """
    Odtwarza `_bot_credentials` z pliku zapisanego przez `_persist_autostart`
    - wołane RAZ przy starcie appki, patrz app/__init__.py::_register_scheduler
    (gate na jeden prawdziwy proces serwujący, żeby nie robić tego dwa razy
    przy reloaderze Werkzeug w trybie debug). Brak pliku (appka jeszcze nigdy
    nie miała aktywnego bota z autostartem, albo świadomie skasowany) -
    cichy no-op, zwraca pustą listę. Zwraca listę odtworzonych user_id, żeby
    wołający mógł od razu wywołać reconcile() dla każdego (ten sam efekt co
    ręczna aktywacja w UI).
    """
    path = os.path.join(instance_path, _AUTOSTART_FILENAME)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        data = json.load(f)
    restored = []
    for uid_str, b64_key in data.items():
        uid = int(uid_str)
        _bot_credentials[uid] = base64.b64decode(b64_key)
        restored.append(uid)
    return restored
