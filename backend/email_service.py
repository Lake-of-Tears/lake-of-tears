from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("lakehouse.email")


def _smtp_cfg() -> dict:
    return {
        "host": os.getenv("SMTP_HOST", ""),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "from_addr": os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "")),
    }


def _send(to: list[str], subject: str, body_html: str) -> None:
    cfg = _smtp_cfg()
    if not cfg["host"] or not to:
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(body_html, "html"))

    try:
        if cfg["port"] == 465:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=10) as s:
                s.ehlo()
                if cfg["user"]:
                    s.login(cfg["user"], cfg["password"])
                s.sendmail(cfg["from_addr"], to, msg.as_string())
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=10) as s:
                s.ehlo()
                if cfg["port"] != 25:
                    s.starttls()
                if cfg["user"]:
                    s.login(cfg["user"], cfg["password"])
                s.sendmail(cfg["from_addr"], to, msg.as_string())
    except Exception as e:
        logger.warning(f"Failed to send email to {to}: {e}")


def send_access_requested(
    owner_emails: list[str],
    requesting_workspace: str,
    catalog_name: str,
    mode: str,
) -> None:
    subject = f"[Lake of Tears] Catalog access request: {requesting_workspace} → {catalog_name}"
    body = f"""
    <p>Workspace <strong>{requesting_workspace}</strong> has requested
    <strong>{mode}</strong> access to catalog <strong>{catalog_name}</strong>.</p>
    <p>Log in to Lake of Tears to approve or reject the request under
    <em>Settings → Catalogs</em>.</p>
    """
    _send(owner_emails, subject, body)


def send_access_reviewed(
    requester_email: str,
    catalog_name: str,
    status: str,
) -> None:
    subject = f"[Lake of Tears] Catalog access {status}: {catalog_name}"
    body = f"""
    <p>Your access request for catalog <strong>{catalog_name}</strong>
    has been <strong>{status}</strong>.</p>
    """
    _send([requester_email], subject, body)


def send_access_removed(
    owner_emails: list[str],
    removing_workspace: str,
    catalog_name: str,
) -> None:
    subject = f"[Lake of Tears] Shared access removed: {removing_workspace} → {catalog_name}"
    body = f"""
    <p>Workspace <strong>{removing_workspace}</strong> has removed their shared access
    to catalog <strong>{catalog_name}</strong>.</p>
    """
    _send(owner_emails, subject, body)


def send_invitation(
    to_email: str,
    role: str,
    inviter_name_or_email: str,
    base_url: str,
) -> None:
    subject = "[Lake of Tears] You've been invited to join Lake of Tears"
    login_url = f"{base_url.rstrip('/')}/login"
    body = f"""
    <p>Hello,</p>
    <p><strong>{inviter_name_or_email}</strong> has invited you to join <strong>Lake of Tears</strong> with <strong>{role.title()}</strong> access.</p>
    <p>To sign in, visit the login page and sign in with your Google, Apple, or SSO account:</p>
    <p><a href="{login_url}" style="display:inline-block;padding:10px 18px;background:#2563eb;color:#fff;text-decoration:none;border-radius:6px;font-weight:bold;">Sign in to Lake of Tears</a></p>
    <p>Or visit: <a href="{login_url}">{login_url}</a></p>
    """
    _send([to_email], subject, body)
