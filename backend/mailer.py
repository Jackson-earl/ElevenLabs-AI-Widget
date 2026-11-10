"""Sends email through an SMTP server: Gmail, Outlook, or a sending service such as Resend or Postmark.

Set these in .env:
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USERNAME=you@gmail.com
    SMTP_PASSWORD=...           for Gmail, an "app password", not your normal password
    MAIL_FROM=Your Name <you@gmail.com>

Without SMTP_HOST, nothing is sent: the email is printed in the worker's terminal instead, so
everything can be tested on your own machine.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("mailer")


def send_email(to, subject, body):
    """Send a plain-text email. Raises smtplib.SMTPException or OSError if sending fails."""
    host = os.environ.get("SMTP_HOST")
    if not host:
        log.info("SMTP_HOST isn't set, so this email was printed instead of sent:\n"
                 "To: %s\nSubject: %s\n\n%s", to, subject, body)
        return

    message = EmailMessage()  # refuses line breaks in headers, so a crafted address can't add its own
    message["From"] = os.environ.get("MAIL_FROM") or os.environ["SMTP_USERNAME"]
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    port = int(os.environ.get("SMTP_PORT", "587"))
    # create_default_context() checks the server's certificate, so nobody in between can read
    # your password by pretending to be the mail server. smtplib alone doesn't check it.
    context = ssl.create_default_context()
    local = host in ("localhost", "127.0.0.1")  # a test mail server on your own machine
    if port == 465:
        # Port 465 is encrypted from the first byte.
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    with server:
        if port != 465 and not local:
            # Port 587 starts unencrypted and switches to encryption with STARTTLS.
            server.starttls(context=context)
        if os.environ.get("SMTP_USERNAME"):
            server.login(os.environ["SMTP_USERNAME"], os.environ.get("SMTP_PASSWORD", ""))
        server.send_message(message)
    log.info("Emailed %s: %s", to, subject)
