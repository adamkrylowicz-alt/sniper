"""
app/routes/help.py
===================
Strona /help - renderuje docs/help_pl.md albo docs/help_en.md (wg
current_lang) jako HTML w base.html. Celowo BEZ @login_required - pomoc ma
być dostępna też dla kogoś kto jeszcze nie ma konta (widoczna z ekranu
logowania), patrz link w base.html.
"""
from __future__ import annotations

import os

from flask import Blueprint, current_app, render_template

from ..i18n import DEFAULT_LANGUAGE, LANG_COOKIE_NAME, SUPPORTED_LANGUAGES
from ..services import simple_markdown

help_bp = Blueprint("help", __name__)


@help_bp.route("/help", methods=["GET"])
def view():
    from flask import request

    lang = request.cookies.get(LANG_COOKIE_NAME)
    if lang not in SUPPORTED_LANGUAGES:
        lang = DEFAULT_LANGUAGE

    docs_dir = os.path.join(os.path.dirname(current_app.root_path), "docs")
    path = os.path.join(docs_dir, f"help_{lang}.md")
    with open(path, encoding="utf-8") as f:
        content_html = simple_markdown.render(f.read())

    return render_template("help.html", content_html=content_html)
