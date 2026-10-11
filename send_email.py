"""Email today's digest. Runs at the end of the daily GitHub Action.

Needs three repository secrets (Settings -> Secrets and variables -> Actions):
  SMTP_USER      sender address, e.g. your Gmail address
  SMTP_PASSWORD  app password for that account (not your normal password)
  MAIL_TO        recipient address (defaults to SMTP_USER)
If any is missing the script prints a note and exits cleanly, so the digest still publishes.
"""
import datetime as dt
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def main():
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    to = os.environ.get("MAIL_TO", "").strip() or user
    if not (user and password and to):
        print("email skipped: SMTP_USER / SMTP_PASSWORD / MAIL_TO not set")
        return

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    page = os.environ.get("PAGE_URL", "https://abhishekfolder-spec.github.io/my-news/")
    today = dt.datetime.now(IST).strftime("%d %b %Y")

    msg = EmailMessage()
    msg["Subject"] = f"The Abhishek Wire · {today}"
    msg["From"] = user
    msg["To"] = to
    msg.set_content(f"Today's digest: {page}\n\nThe full dashboard is attached and also opens in a browser.")

    html = Path("docs/index.html")
    if html.exists():
        msg.add_attachment(html.read_text(encoding="utf-8"), subtype="html",
                           filename=f"wire-{dt.datetime.now(IST):%Y-%m-%d}.html")

    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60) as s:
        s.login(user, password)
        s.send_message(msg)
    print(f"email sent to {to}")


if __name__ == "__main__":
    main()
