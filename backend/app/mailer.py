"""Outbound mail, stdlib smtplib only. Configure with env vars:

  PLSEM_SMTP_HOST      e.g. smtp.gmail.com — mail is disabled while unset
  PLSEM_SMTP_PORT      default 587 (STARTTLS)
  PLSEM_SMTP_USER      SMTP login (e.g. the Gmail address)
  PLSEM_SMTP_PASSWORD  SMTP password (for Gmail: an app password)
  PLSEM_SMTP_FROM      From: header, defaults to PLSEM_SMTP_USER

Mail is best-effort: it goes out on a daemon thread and failures are logged,
never raised — signup must not break because a welcome mail bounced. While
SMTP is unconfigured the message is printed to the server log instead, so the
flows (welcome mail, password-reset link) are testable locally without an
SMTP account.
"""
import logging
import os
import smtplib
import threading
from email.message import EmailMessage

log = logging.getLogger("plsem.mailer")


def is_configured() -> bool:
    return bool(os.environ.get("PLSEM_SMTP_HOST"))


def _deliver(msg: EmailMessage) -> None:
    host = os.environ["PLSEM_SMTP_HOST"]
    port = int(os.environ.get("PLSEM_SMTP_PORT", "587"))
    user = os.environ.get("PLSEM_SMTP_USER")
    password = os.environ.get("PLSEM_SMTP_PASSWORD")
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
        log.info("sent %r to %s", msg["Subject"], msg["To"])
    except Exception:
        log.exception("could not send %r to %s", msg["Subject"], msg["To"])


def send(to: str, subject: str, body: str) -> None:
    """Queue one plain-text mail; returns immediately."""
    if not is_configured():
        # Local/dev fallback: surface the mail (incl. any reset link) in the log.
        print(f"[mailer] SMTP not configured — would send to {to}:\n"
              f"  Subject: {subject}\n" + "".join(f"  {l}\n" for l in body.splitlines()),
              flush=True)
        return
    msg = EmailMessage()
    msg["From"] = os.environ.get("PLSEM_SMTP_FROM") or os.environ.get("PLSEM_SMTP_USER")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    threading.Thread(target=_deliver, args=(msg,), daemon=True).start()
