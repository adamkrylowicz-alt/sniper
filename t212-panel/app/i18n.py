"""
app/i18n.py
===========
Płaski i18n: polski tekst UŻYTY W KODZIE JEST kluczem słownika, więc dla
lang="pl" (domyślny) t() zwraca tekst bez zmian - zero pliku potrzebne.
Dla innych języków tłumaczenie wczytywane jest z osobnego pliku tekstowego
app/translations/<lang>.txt, format jednej linii:

    Polski tekst|||English text

"|||" zamiast "=" - polskie zdania w UI czasem zawierają znak "=" (kwoty,
wzory), więc zwykły format klucz=wartość by się rozjechał.

Plik en.txt jest celowo POZA szablonami/kodem - to on jest tym "osobnym
plikiem txt dogrywanym na serwer" (Adam, 10.08.2026): dograć nowe/zmienione
tłumaczenia można samą podmianą tego jednego pliku, bez ruszania reszty appki.
"""
from __future__ import annotations

import os

SUPPORTED_LANGUAGES = ("pl", "en")
DEFAULT_LANGUAGE = "pl"
LANG_COOKIE_NAME = "snajper_lang"

_TRANSLATIONS_DIR = os.path.join(os.path.dirname(__file__), "translations")
_cache: dict[str, dict[str, str]] = {}


def _load(lang: str) -> dict[str, str]:
    if lang in _cache:
        return _cache[lang]
    path = os.path.join(_TRANSLATIONS_DIR, f"{lang}.txt")
    table: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.rstrip("\n")
                if not line or line.startswith("#") or "|||" not in line:
                    continue
                key, _, value = line.partition("|||")
                table[key] = value
    _cache[lang] = table
    return table


def t(text: str, lang: str = DEFAULT_LANGUAGE) -> str:
    if not text or lang == DEFAULT_LANGUAGE:
        return text
    return _load(lang).get(text, text)


def all_translations(lang: str) -> dict[str, str]:
    """Cały słownik danego języka - używane do wstrzyknięcia do JS w base.html."""
    if lang == DEFAULT_LANGUAGE:
        return {}
    return _load(lang)


def current_lang() -> str:
    """
    To samo rozstrzyganie języka co context processor w __init__.py
    (ciasteczko wygrywa, potem UserSettings.language, potem 'pl') - ale
    dostępne też POZA renderem szablonu, np. we flash() w routes/*.py,
    gdzie nie ma jinja-globala t().
    """
    from flask import g, request

    lang = request.cookies.get(LANG_COOKIE_NAME)
    if lang in SUPPORTED_LANGUAGES:
        return lang

    user_id = getattr(g, "user_id", None)
    if user_id is not None:
        from .models import UserSettings
        settings = UserSettings.query.filter_by(user_id=user_id).first()
        if settings is not None and settings.language in SUPPORTED_LANGUAGES:
            return settings.language

    return DEFAULT_LANGUAGE
