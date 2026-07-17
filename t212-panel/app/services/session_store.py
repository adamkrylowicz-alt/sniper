"""
app/services/session_store.py
==============================
Serwerowe (in-memory) przechowywanie zalogowanych sesji.

DLACZEGO NIE flask.session (domyślne ciasteczko)?
----------------------------------------------------
Domyślne ciasteczko sesji Flaska jest PODPISANE (nie da się go sfałszować),
ale NIE jest zaszyfrowane - klient (przeglądarka, i teoretycznie każdy kto
przechwyci ciasteczko/ma dostęp do przeglądarki) może je zdekodować i
PRZECZYTAĆ zawartość. Trzymanie tam odszyfrowanego master_key (klucza do
sekretów API T212) byłoby złamaniem całego modelu Zero-Knowledge.

Dlatego: ciasteczko dostaje tylko losowy, nieznaczący TOKEN. Prawdziwe dane
sesji (user_id, master_key) siedzą w słowniku w pamięci procesu Flaska,
kluczowanym tym tokenem.

ZNANE OGRANICZENIE (świadomy kompromis na tym etapie):
Pamięć procesu = restart appki (python3 run.py) wylogowuje WSZYSTKICH.
Przy jednym użytkowniku na własnym NAS-ie to akceptowalne. Gdyby kiedyś
appka miała działać pod wieloma workerami (gunicorn -w 4) - ten słownik
przestanie działać poprawnie (każdy worker ma swoją pamięć) - wtedy trzeba
przejść na Redis. Zostawiam to jako jawną notatkę, nie próbuję tego
rozwiązywać na zapas.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

SESSION_COOKIE_NAME = "sniper_session"
SESSION_TTL_SECONDS = 60 * 60 * 4  # 4 godziny nieaktywności -> wylogowanie


@dataclass
class SessionData:
    user_id: int
    master_key: bytes
    expires_at: float
    ttl_seconds: int = SESSION_TTL_SECONDS


_sessions: dict[str, SessionData] = {}


def create_session(user_id: int, master_key: bytes, ttl_seconds: int | None = None) -> str:
    """
    Tworzy nową sesję, zwraca token do zapisania w ciasteczku.
    ttl_seconds=None -> domyślny SESSION_TTL_SECONDS (4h). Dłuższy czas
    (np. 30 dni) używany gdy user zaznaczy "zapamiętaj mnie" przy logowaniu.
    """
    token = secrets.token_urlsafe(32)
    ttl = ttl_seconds if ttl_seconds is not None else SESSION_TTL_SECONDS
    _sessions[token] = SessionData(
        user_id=user_id,
        master_key=master_key,
        expires_at=time.monotonic() + ttl,
        ttl_seconds=ttl,
    )
    return token


def get_session(token: str | None) -> SessionData | None:
    """Zwraca dane sesji jeśli token istnieje i nie wygasł, inaczej None."""
    if not token:
        return None
    data = _sessions.get(token)
    if data is None:
        return None
    if time.monotonic() > data.expires_at:
        _sessions.pop(token, None)
        return None
    # Przedłużenie sesji przy aktywności (rolling expiry) - własnym ttl,
    # nie sztywnym SESSION_TTL_SECONDS, inaczej sesja "zapamiętaj mnie"
    # (30 dni) zjeżdżałaby z powrotem do 4h przy pierwszym kolejnym requeście.
    data.expires_at = time.monotonic() + data.ttl_seconds
    return data


def destroy_session(token: str | None) -> None:
    """Usuwa sesję (wylogowanie) - bezpieczne nawet jeśli token nie istnieje."""
    if token:
        _sessions.pop(token, None)
