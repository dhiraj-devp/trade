"""Optional Telegram and email notices. The scanner runs without either."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

import requests

from nse_scanner.redact import public_text
from nse_scanner.secrets import load_secrets


def send_telegram(text: str) -> tuple[bool, str]:
    secrets = load_secrets()
    token = secrets.get("telegram_token") or ""
    from nse_scanner.settings import load_settings
    chat_id = load_settings().get("telegram_chat_id") or ""
    if not token or not chat_id:
        return False, "Telegram token or chat id is missing."
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if response.ok:
            return True, "Telegram message sent."
        return False, f"Telegram returned status {response.status_code}."
    except Exception as exc:
        return False, public_text(str(exc))


def send_email(subject: str, text: str) -> tuple[bool, str]:
    from nse_scanner.settings import load_settings
    settings = load_settings()
    secrets = load_secrets()
    password = secrets.get("smtp_password") or ""
    host = settings.get("smtp_host") or ""
    username = settings.get("smtp_username") or ""
    sender = settings.get("smtp_from") or username
    recipient = settings.get("smtp_to") or ""
    if not all((host, username, password, sender, recipient)):
        return False, "Email settings are incomplete."
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(text)
    try:
        with smtplib.SMTP(host, int(settings.get("smtp_port") or 587), timeout=15) as server:
            server.starttls()
            server.login(username, password)
            server.send_message(message)
        return True, "Email sent."
    except Exception as exc:
        return False, public_text(str(exc))


def notify_all(settings: dict, text: str) -> list[str]:
    notes = []
    if settings.get("telegram_enabled"):
        ok, message = send_telegram(text)
        notes.append(f"Telegram: {'sent' if ok else message}")
    if settings.get("email_enabled"):
        ok, message = send_email("NSE F&O scanner signal", text)
        notes.append(f"Email: {'sent' if ok else message}")
    if not notes:
        notes.append("Dashboard only. No external notification provider is enabled.")
    return notes
