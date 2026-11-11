"""Builds agents for queued requests, one at a time.

Run it in its own terminal, next to the web app, from the repository root:
    python backend/worker.py

It checks the database every few seconds. Stop it with Ctrl+C.

Intake requests end at 'ready_for_review' for you to check. Demo requests (from the free demo
form) are finished here instead: the worker takes a screenshot of the homepage, makes a secret
link, and emails it to the person who asked.

It also writes the common questions for each new industry a client names, as a draft for you to
approve on the admin's Industries page. Builds use an industry's questions once you approve them.
"""

import logging
import os
import re
import secrets
import smtplib
import time
import traceback

import db
from build_agent import (DEMO_DAYS, PUBLIC_URL, BuildError, add_industry_doc, build, facts_text,
                         industry_doc_entry)
from industry import IndustryError, write_questions
from mailer import send_email

POLL_SECONDS = 5
# uploads/ sits in the repository root, one folder up from this file.
SCREENSHOTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads", "demos")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("worker")


def recover_interrupted():
    """A request still marked 'building' when the worker starts was cut off by a crash or Ctrl+C.

    Restarting it automatically would start a second crawl and leave the first crawl's folder
    behind, so mark it failed and let a person decide whether to rebuild it."""
    for request in db.list_requests():
        if request["status"] == "building":
            db.update(request["id"], status="failed",
                      error="The worker stopped during this build. Rebuild it from the admin page.")
            log.info("Request %s was interrupted mid-build; marked failed", request["id"])


COOKIE_BUTTON = re.compile(r"^\s*(accept( all)?( cookies)?|allow( all)?( cookies)?|i agree|agree|got it|ok(ay)?)\s*$", re.I)


def dismiss_cookie_banner(page):
    """Click a cookie popup's accept button, if there is one, so it isn't in the screenshot.

    The pattern must match the button's whole label, so "Accept payments" is never clicked.
    Some consent popups live in an iframe, so every frame is checked."""
    for frame in page.frames:
        try:
            button = frame.get_by_role("button", name=COOKIE_BUTTON).first
            if button.is_visible():
                button.click(timeout=2_000)
                page.wait_for_timeout(1_000)  # let the popup's closing animation finish
                return
        except Exception:  # a frame that went away or a button that won't click: no harm done
            continue


def take_screenshot(url, path):
    """Save a picture of the top of a homepage for the demo page, and return its path.

    Optional: returns None if Playwright isn't installed or the page won't load, and the demo
    page then uses a plain background. Install it with:
        pip install playwright && playwright install chromium"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.info("Playwright isn't installed, so the demo page will have no screenshot")
        return None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                # A full-HD desktop window. The demo page shows the picture at this size and only
                # shrinks it for smaller windows, so it should be at least as wide as most screens.
                page = browser.new_page(viewport={"width": 1920, "height": 1080})
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(3_000)  # let fonts, images and entrance animations settle
                dismiss_cookie_banner(page)
                # JPEG is about 10 times smaller than PNG for a photo-heavy page, so the demo loads fast.
                page.screenshot(path=path, type="jpeg", quality=80)
            finally:
                browser.close()
        return path
    except Exception as e:  # any browser problem only costs us the picture, not the demo
        log.info("Couldn't take a screenshot of %s (%s); continuing without one", url, e)
        return None


def demo_email(request, link):
    return f"""Hi,

Here's the free demo you asked for: an AI assistant that has read {request['company']}'s website and answers questions about it.

See it on your homepage: {link}

Click the button in the bottom-right corner and ask it what a customer would: what you offer, what it costs, when you're open. You can talk to it or type.

The link works for {DEMO_DAYS} days. If you'd like the assistant on your real website, there's a button on the demo page to get started.

Thanks,
"""


def fail(request, error):
    db.update(request["id"], status="failed", error=error)
    if request["kind"] != "demo":
        return
    # Someone is waiting for a demo email, so tell them instead of leaving them wondering.
    # The real reason stays in the admin page; they get a plain message.
    try:
        send_email(request["contact_email"], "About your assistant demo", f"""Hi,

Thanks for asking for a demo. We couldn't build one for {request['website']} automatically. Some websites block automated visitors or need a little setup by hand.

We've been notified and will get in touch to set it up for you.

Thanks,
""")
    except (smtplib.SMTPException, OSError):
        log.exception("Couldn't email request %s about its failed demo", request["id"])


def finish_demo(request, result):
    request_id = request["id"]
    # Keep the token on a rebuild, so a link that was already emailed keeps working.
    token = request["demo_token"] or secrets.token_urlsafe(16)
    screenshot = take_screenshot(request["website"], os.path.join(SCREENSHOTS, f"{token}.jpg"))
    link = f"{PUBLIC_URL}/demo/{token}"
    # Save the finished demo before emailing, so the link works the moment the email arrives.
    db.update(request_id, status="demo_sent", error=None, demo_token=token, screenshot=screenshot,
              demo_expires_at=time.time() + DEMO_DAYS * 24 * 3600, **result)
    try:
        send_email(request["contact_email"], f"Your {request['company']} assistant demo is ready", demo_email(request, link))
    except (smtplib.SMTPException, OSError) as e:
        db.update(request_id, status="failed",
                  error=f"The demo was built, but the email didn't send ({e}). Its link is {link}")
        log.exception("Request %s: demo built but the email failed", request_id)
        return
    log.info("Request %s: demo emailed to %s (%s)", request_id, request["contact_email"], link)


def write_new_industries():
    """Write the draft questions for industries that have none yet. A failure is saved on the
    industry, so it isn't retried every few seconds; the admin page's Write button retries it."""
    for industry in db.list_industries():
        if industry["status"] != "draft" or industry["questions"] or industry["error"]:
            continue
        log.info("Writing the common questions for %s", industry["name"])
        try:
            db.update_industry(industry["id"], questions=write_questions(industry["name"]))
            log.info("Questions for %s are ready for your approval", industry["name"])
        except IndustryError as e:
            db.update_industry(industry["id"], error=str(e))
            log.info("Couldn't write the questions for %s: %s", industry["name"], e)
        except Exception:
            db.update_industry(industry["id"], error="Unexpected error:\n" + traceback.format_exc())
            log.exception("Writing the questions for %s crashed", industry["name"])


def approved_industry(request):
    """The request's industry if its questions are approved and uploaded, else None."""
    industry = request["industry_id"] and db.get_industry(request["industry_id"])
    return industry if industry and industry["status"] == "approved" and industry["doc_id"] else None


def process(request):
    request_id = request["id"]
    demo = request["kind"] == "demo"
    log.info("Building %s %s: %s (%s)", "demo" if demo else "request", request_id, request["company"], request["website"])

    def on_progress(message):
        log.info("  [%s] %s", request_id, message)
        db.update(request_id, progress=message)

    industry = approved_industry(request)
    try:
        result = build(
            request["company"],
            request["website"],
            facts=facts_text(request["facts"]),
            files=request["files"],
            demo=demo,
            # Set when the intake form found a demo of this site: its crawl is reused.
            folder_id=request["folder_id"],
            industry_doc=industry and industry_doc_entry(industry["name"], industry["doc_id"]),
            on_progress=on_progress,
        )
    except BuildError as e:
        # An expected problem, such as a blocked site or a rejected file. The message is readable as-is.
        log.info("Request %s failed: %s", request_id, e)
        fail(request, str(e))
        return
    except Exception:
        # A bug or a network outage. Save the full traceback so you can debug it, and keep running.
        log.exception("Request %s crashed", request_id)
        fail(request, "Unexpected error:\n" + traceback.format_exc())
        return

    if demo:
        finish_demo(request, result)
        return
    # The industry may have been approved during the build. Approving adds the guide only to
    # agents that existed then, so add it to this one now.
    late = approved_industry(request)
    if late and not industry:
        try:
            add_industry_doc(result["agent_id"], late["name"], late["doc_id"])
        except BuildError as e:
            log.info("Request %s: couldn't add the %s industry guide (%s)", request_id, late["name"], e)
    db.update(request_id, status="ready_for_review", error=None, **result)
    log.info("Request %s is ready for review (agent %s)", request_id, result["agent_id"])


def main():
    recover_interrupted()
    log.info("Worker started; checking for requests every %s seconds", POLL_SECONDS)
    while True:
        write_new_industries()
        request = db.claim_next()
        if request is None:
            time.sleep(POLL_SECONDS)
            continue
        process(request)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Worker stopped")
