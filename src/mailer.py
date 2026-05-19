"""
Gmail SMTP sender.

Sends the weekly report as an HTML email with the PDF attached.

Auth: Gmail app password (16 chars). Requires 2FA on the Google account.
  https://myaccount.google.com/apppasswords

Credentials are read from environment variables (so they live in GitHub
Secrets, never in the repo):
  GMAIL_ADDRESS    -- the sending Gmail address
  GMAIL_APP_PASSWORD -- the app password (no spaces)

Recipients and display name come from config/recipients.yaml.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465   # SSL; alternative is 587 with STARTTLS


def _build_message(
    sender_email: str,
    sender_name: str,
    recipients: list[dict],
    subject: str,
    html_body: str,
    pdf_path: Path | None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender_email}>"
    # Put recipients in BCC so each person sees only their own address.
    # The visible To header points back to the sender.
    msg["To"] = sender_email
    msg["Bcc"] = ", ".join(r["email"] for r in recipients)

    # Plain-text fallback for clients that block HTML.
    msg.set_content(
        "Your email client does not appear to render HTML. "
        "Please open the attached PDF for the full report."
    )
    # The HTML version is the primary body.
    msg.add_alternative(html_body, subtype="html")

    if pdf_path and pdf_path.exists():
        ctype, _ = mimetypes.guess_type(pdf_path.name)
        if not ctype:
            ctype = "application/pdf"
        maintype, subtype = ctype.split("/", 1)
        with open(pdf_path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype=maintype,
                subtype=subtype,
                filename=pdf_path.name,
            )
        logger.info("Attached PDF: %s (%d bytes)", pdf_path.name, pdf_path.stat().st_size)
    else:
        logger.warning("No PDF attached (path missing or not found)")

    return msg


def send_report(
    recipients_config: dict,
    subject: str,
    html_body: str,
    pdf_path: Path | str | None,
) -> None:
    """
    recipients_config: parsed contents of recipients.yaml
    Raises smtplib.SMTPException on failure -- caller decides what to do.
    """
    sender = recipients_config["sender"]
    sender_email = sender["email"]
    sender_name = sender.get("display_name", "6G Weekly Survey")

    # Allow override from env for CI; fall back to YAML.
    sender_email = os.environ.get("GMAIL_ADDRESS", sender_email)
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not app_password:
        raise RuntimeError(
            "GMAIL_APP_PASSWORD environment variable not set. "
            "Create one at https://myaccount.google.com/apppasswords"
        )

    recipients = recipients_config.get("recipients", [])
    if not recipients:
        raise RuntimeError("No recipients configured in recipients.yaml")

    pdf_path = Path(pdf_path) if pdf_path else None
    msg = _build_message(sender_email, sender_name, recipients, subject, html_body, pdf_path)

    logger.info("Sending to %d recipient(s) via %s...", len(recipients), SMTP_HOST)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60) as smtp:
        smtp.login(sender_email, app_password)
        # send_message handles To/Cc/Bcc routing correctly.
        smtp.send_message(msg)
    logger.info("Email sent.")


if __name__ == "__main__":
    # Offline smoke test: build the message without sending.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    fake_cfg = {
        "sender": {"email": "test@gmail.com", "display_name": "Test Bot"},
        "recipients": [
            {"name": "Alice", "email": "alice@example.com"},
            {"name": "Bob", "email": "bob@example.com"},
        ],
    }
    msg = _build_message(
        sender_email="test@gmail.com",
        sender_name="Test Bot",
        recipients=fake_cfg["recipients"],
        subject="Test subject",
        html_body="<html><body><h1>Hi</h1></body></html>",
        pdf_path=None,
    )
    print("Subject:", msg["Subject"])
    print("From:   ", msg["From"])
    print("To:     ", msg["To"])
    print("Bcc:    ", msg["Bcc"])
    print("Parts:  ", [p.get_content_type() for p in msg.walk()])
