"""
app/routes/report.py
=====================
"Zgłoś problem" - przyjmuje opis + opcjonalny załącznik (screenshot/txt),
dokleja automatycznie: kto zgłasza, kiedy, jaki adres strony, i ostatnie
błędy JS złapane w przeglądarce (patrz common.js - collectErrorLog()) -
żeby przyspieszyć diagnozę bez proszenia usera o ręczne kopiowanie konsoli.
"""

from __future__ import annotations

import datetime as dt

from flask import Blueprint, current_app, jsonify, request

from ..models import User
from ..services.mailer import MailerNotConfiguredError, MailSendError, send_report
from ..utils import current_user_id, login_required

report_bp = Blueprint("report", __name__, url_prefix="/report")

MAX_ATTACHMENT_SIZE = 5 * 1024 * 1024  # 5 MB
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "txt", "log"}


@report_bp.route("/submit", methods=["POST"])
@login_required
def submit():
    description = (request.form.get("description") or "").strip()
    page_url = (request.form.get("page_url") or "").strip()
    browser_errors = (request.form.get("browser_errors") or "").strip()

    if not description:
        return jsonify(ok=False, error="Opisz problem zanim wyślesz."), 400

    user = User.query.get(current_user_id())
    username = user.username if user else f"user_id={current_user_id()}"

    attachment_filename = None
    attachment_bytes = None
    uploaded = request.files.get("attachment")
    if uploaded and uploaded.filename:
        ext = uploaded.filename.rsplit(".", 1)[-1].lower() if "." in uploaded.filename else ""
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify(
                ok=False,
                error=f"Niedozwolony typ pliku (.{ext}) - dozwolone: {', '.join(sorted(ALLOWED_EXTENSIONS))}.",
            ), 400

        attachment_bytes = uploaded.read()
        if len(attachment_bytes) > MAX_ATTACHMENT_SIZE:
            return jsonify(ok=False, error="Plik za duży (max 5 MB)."), 400
        attachment_filename = uploaded.filename

    timestamp = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    body_lines = [
        f"Zgłoszenie od: {username}",
        f"Czas: {timestamp}",
        f"Strona: {page_url or '(brak)'}",
        "",
        "--- Opis problemu ---",
        description,
    ]
    if browser_errors:
        body_lines += ["", "--- Ostatnie błędy JS w przeglądarce (automatycznie) ---", browser_errors]

    body_text = "\n".join(body_lines)

    try:
        send_report(
            smtp_host=current_app.config["SMTP_HOST"],
            smtp_port=current_app.config["SMTP_PORT"],
            smtp_user=current_app.config["SMTP_USER"],
            smtp_password=current_app.config["SMTP_PASSWORD"],
            mail_from=current_app.config["SMTP_FROM"],
            mail_to=current_app.config["REPORT_TO_EMAIL"],
            subject=f"[SNIPER] Zgłoszenie od {username}",
            body_text=body_text,
            attachment_filename=attachment_filename,
            attachment_bytes=attachment_bytes,
        )
    except MailerNotConfiguredError as exc:
        return jsonify(ok=False, error=str(exc)), 500
    except MailSendError as exc:
        return jsonify(ok=False, error=str(exc)), 502

    return jsonify(ok=True)
