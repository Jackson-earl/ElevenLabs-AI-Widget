"""The web app: the intake form clients fill in, and the admin pages where you review agents.

Run it from the repository root with:
    flask --app backend/app run --debug --port 5001
then open http://127.0.0.1:5001 (intake form) or http://127.0.0.1:5001/admin (admin pages).

(Port 5000 is taken by AirPlay Receiver on most Macs, hence 5001.)

Requires: pip install flask
"""

import hmac
import os
import re
import shutil
import time
import uuid
from urllib.parse import urlparse

from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

import db
from build_agent import (DEMO_DAYS, FACT_FIELDS, FILE_TYPES, MAX_FILE_BYTES, PUBLIC_URL, BuildError, check_site,
                         company_name, embed_code, facts_text, set_allowed_domains, update_facts)

# The repository root, one folder up from this file. templates/, static/ and uploads/ are there.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOADS = os.path.join(ROOT, "uploads")
MAX_FILES = 5

app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"), static_folder=os.path.join(ROOT, "static"))
# Flask rejects any submission bigger than this before our code runs.
app.config["MAX_CONTENT_LENGTH"] = MAX_FILES * MAX_FILE_BYTES + 1024 * 1024
# flash() keeps its messages in a signed cookie, which needs a secret key. A random one is fine
# for testing; set SECRET_KEY in .env once the app runs on a real server.
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)


@app.get("/")
def intake():
    # request.args are the values after "?" in the address. The demo page links here with
    # ?company=...&website=...&email=... so the form starts filled in.
    return render_template("base/intake.html", fields=FACT_FIELDS, form=request.args, error=None)


@app.post("/")
def submit():
    form = request.form

    def show_error(message):
        # Show the form again with the client's answers still filled in.
        return render_template("base/intake.html", fields=FACT_FIELDS, form=form, error=message), 400

    company = form.get("company", "").strip()
    website = form.get("website", "").strip()
    email = form.get("email", "").strip()
    if not company or not website or not email:
        return show_error("Please fill in your company name, website, and email.")
    if "@" not in email or "." not in email.split("@")[-1]:
        return show_error("That email address doesn't look right.")

    uploads = [f for f in request.files.getlist("files") if f.filename]
    if len(uploads) > MAX_FILES:
        return show_error(f"Please upload at most {MAX_FILES} files.")
    for f in uploads:
        if os.path.splitext(f.filename)[1].lower() not in FILE_TYPES:
            return show_error(f"{f.filename}: please upload PDF, Word (.docx), text, Markdown, HTML, or EPUB files.")

    # Check the website now, so the client hears about a problem while they are still here,
    # not in an email hours later.
    try:
        website, _, problem = check_site(website)
    except BuildError as e:
        return show_error(str(e))
    if problem:
        # check_site's message is written for us (it says "ask the client..."), so the client gets a plain one.
        app.logger.warning("Rejected %s: %s", website, problem)
        return show_error("Our system couldn't read your website. It may block automated visitors or need "
                          "JavaScript to show its text. Contact us and we'll set your assistant up by hand.")

    # Save files into a new folder with a random name, so two clients uploading "pricing.pdf"
    # never overwrite each other. secure_filename strips paths like "../../app.py" from names.
    folder = os.path.join(UPLOADS, uuid.uuid4().hex)
    paths = []
    for f in uploads:
        os.makedirs(folder, exist_ok=True)
        extension = os.path.splitext(f.filename)[1].lower()
        name = secure_filename(f.filename)
        if not name.lower().endswith(extension):  # "价格.pdf" becomes just "pdf"
            name = "upload" + extension
        path = os.path.join(folder, name)
        f.save(path)
        if os.path.getsize(path) > MAX_FILE_BYTES:
            shutil.rmtree(folder)  # don't keep files from a submission we turned down
            return show_error(f"{f.filename} is larger than 20 MB.")
        paths.append(path)

    facts = {key: form.get(key, "").strip() for key, _, _ in FACT_FIELDS if form.get(key, "").strip()}
    db.add_request(company, website, email, facts=facts, files=paths, folder_id=demo_crawl(website))
    # Redirect after a successful POST, so refreshing the thanks page cannot submit twice.
    return redirect(url_for("thanks"))


# ---------------------------------------------------------------- Free demo

# Every demo costs you a crawl and agent minutes, so cap how many strangers can start per day.
MAX_DEMOS_PER_DAY = int(os.environ.get("MAX_DEMOS_PER_DAY", "20"))
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def site_key(website):
    """The part of an address that identifies a website: https://www.acme.com/about -> acme.com."""
    return (urlparse(website).hostname or "").removeprefix("www.")


def demo_crawl(website):
    """The crawl folder of the newest demo of this website, or None.

    Demos crawl the whole site, so a client who signs up after their demo gets an agent built
    from that crawl instead of waiting for, and paying for, the same crawl again. The address
    must match apart from "www." and a trailing slash, because a section address such as
    acme.com/help crawls only that section."""
    def page(url):
        return site_key(url), urlparse(url).path.rstrip("/")
    demos = [d for d in db.recent_demos(since=0) if d["folder_id"] and page(d["website"]) == page(website)]
    return max(demos, key=lambda d: d["id"])["folder_id"] if demos else None


@app.get("/demo")
def demo_form():
    return render_template("demo/demo_form.html", form=request.args, error=None)


@app.post("/demo")
def demo_submit():
    form = request.form

    def show_error(message):
        return render_template("demo/demo_form.html", form=form, error=message), 400

    # A field people never see (it's moved off-screen). Bots fill in every field they find, so a
    # value here means a bot: pretend it worked and do nothing.
    if form.get("leave_empty"):
        return redirect(url_for("demo_thanks"))

    website = form.get("website", "").strip()
    email = form.get("email", "").strip()
    if not website or not email:
        return show_error("Please enter your website and your email.")
    if not EMAIL.fullmatch(email):
        return show_error("That email address doesn't look right.")

    try:
        website, _, problem = check_site(website)
    except BuildError as e:
        return show_error(str(e))
    if problem:
        app.logger.warning("Demo refused for %s: %s", website, problem)
        return show_error("Our system couldn't read your website. It may block automated visitors or need "
                          "JavaScript to show its text. Contact us and we'll set up a demo by hand.")

    now = time.time()
    recent = db.recent_demos(since=now - DEMO_DAYS * 24 * 3600)
    if any(site_key(d["website"]) == site_key(website) and d["status"] not in ("failed", "rejected") for d in recent):
        return show_error("We've already made a demo for this website in the last week, and emailed it to the "
                          "address entered then. Contact us if you need it sent again.")
    if sum(d["created_at"] > now - 24 * 3600 for d in recent) >= MAX_DEMOS_PER_DAY:
        return show_error("We've reached today's limit for free demos. Please try again tomorrow.")

    db.add_request(company_name(website), website, email, kind="demo")
    return redirect(url_for("demo_thanks"))


@app.get("/demo/thanks")
def demo_thanks():
    return render_template("demo/demo_thanks.html")


def live_demo_or_404(token):
    """The demo for this token, or a 404 page. 404 also covers a demo being rebuilt."""
    demo = db.get_demo(token)
    if demo is None or demo["status"] != "demo_sent":
        abort(404)
    return demo


@app.get("/demo/<token>")
def demo_page(token):
    demo = live_demo_or_404(token)
    if time.time() > demo["demo_expires_at"]:
        # 410 means "gone": this address worked once and won't again.
        return render_template("demo/demo_expired.html", r=demo), 410
    return render_template("demo/demo.html", r=demo, has_screenshot=bool(demo["screenshot"]))


@app.get("/demo/<token>/screenshot.jpg")
def demo_screenshot(token):
    # Screenshots are served through this route, never straight from the uploads folder,
    # so only a valid, unexpired token shows one.
    demo = live_demo_or_404(token)
    if time.time() > demo["demo_expires_at"] or not demo["screenshot"] or not os.path.isfile(demo["screenshot"]):
        abort(404)
    return send_file(demo["screenshot"], mimetype="image/jpeg")


@app.get("/thanks")
def thanks():
    return render_template("base/thanks.html")


@app.errorhandler(413)
def too_large(_):
    return render_template("base/intake.html", fields=FACT_FIELDS, form={},
                           error="Those files are too large together. Please upload less, or smaller files."), 413


# ---------------------------------------------------------------- Admin pages


@app.before_request
def require_admin_password():
    """Every page under /admin needs the password from ADMIN_PASSWORD. Runs before each request."""
    if not request.path.startswith("/admin"):
        return None
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        return "Set ADMIN_PASSWORD in .env to use the admin pages.", 503
    given = request.authorization.password if request.authorization else None
    # compare_digest takes the same time however many characters match, so the timing of
    # failed attempts gives away nothing about the real password.
    if given and hmac.compare_digest(given.encode(), password.encode()):
        return None
    # A 401 with this header makes the browser show its built-in username and password box.
    return Response("Admin login required", 401, {"WWW-Authenticate": 'Basic realm="Admin"'})


@app.template_filter("ago")
def ago(timestamp):
    minutes = int((time.time() - timestamp) / 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (24 * 60)} days ago"


@app.template_filter("date")
def date(timestamp):
    return time.strftime("%b %d, %Y", time.localtime(timestamp))


# Available in every template, for building full demo links on the admin pages.
app.jinja_env.globals["PUBLIC_URL"] = PUBLIC_URL


# Which statuses each action is allowed from. For example, a request being built can't be
# rebuilt or approved until the worker has finished with it.
ACTIONS = {
    "approve": ({"ready_for_review"}, "approved"),
    "reject": ({"ready_for_review", "failed"}, "rejected"),
    "rebuild": ({"ready_for_review", "failed", "rejected"}, "queued"),
}


def default_domains(website):
    """The domains to suggest on the Approve form: the client's site with and without "www."."""
    host = urlparse(website).hostname or ""
    bare = host.removeprefix("www.")
    # Add "www." only to a main domain (acme.com), not a subdomain (shop.acme.com).
    if host.startswith("www.") or bare.count(".") == 1:
        return [bare, "www." + bare]
    return [host]


def parse_domains(text):
    """Turn "acme.com, https://www.acme.com/contact" into ["acme.com", "www.acme.com"].
    Raises ValueError with a readable message for anything that isn't a domain."""
    domains = []
    for item in re.split(r"[\s,]+", text.strip()):
        if not item:
            continue
        host = urlparse(item if "://" in item else "https://" + item).hostname or ""
        if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", host):
            raise ValueError(f"{item} isn't a domain like example.com.")
        domains.append(host)
    if not domains:
        raise ValueError("Enter at least one domain the client will put the widget on.")
    return list(dict.fromkeys(domains))


def get_or_404(request_id):
    found = db.get_request(request_id)
    if found is None:
        abort(404)
    return found


@app.get("/admin")
def admin_list():
    rows = db.list_requests()
    busy = any(r["status"] in ("queued", "building") for r in rows)
    return render_template("admin/admin_list.html", rows=rows, refresh=busy)


@app.get("/admin/requests/<int:request_id>")
def admin_review(request_id):
    found = get_or_404(request_id)
    files = [os.path.basename(path) for path in found["files"]]
    return render_template("admin/admin_review.html", r=found, files=files, fields=FACT_FIELDS,
                           actions=[name for name, (allowed, _) in ACTIONS.items() if found["status"] in allowed],
                           domains=", ".join(default_domains(found["website"])),
                           refresh=found["status"] in ("queued", "building"))


@app.get("/admin/requests/<int:request_id>/delivery")
def admin_delivery(request_id):
    found = get_or_404(request_id)
    if found["status"] != "approved":
        flash("Approve this request before delivering it.", "error")
        return redirect(url_for("admin_review", request_id=request_id))
    embed = embed_code(found["agent_id"])
    # delivery_email.txt ends in .txt, so Flask doesn't HTML-escape it: the embed code stays
    # exactly as the client must paste it. The HTML page escapes it when showing it.
    message = render_template("admin/delivery_email.txt", r=found, embed=embed)
    return render_template("admin/admin_delivery.html", r=found, embed=embed, message=message,
                           subject=f"Your {found['company']} website assistant is ready")


@app.post("/admin/requests/<int:request_id>/<action>")
def admin_action(request_id, action):
    if action not in ACTIONS:
        abort(404)
    found = get_or_404(request_id)
    allowed, new_status = ACTIONS[action]
    if found["status"] not in allowed:
        flash(f"Can't {action} a request that is {found['status'].replace('_', ' ')}.", "error")
    elif action == "approve":
        # Lock the widget to the client's site in ElevenLabs first, and only mark the request
        # approved if that worked: the same outside-service-first rule as saving facts.
        try:
            domains = parse_domains(request.form.get("domains", ""))
            set_allowed_domains(found["agent_id"], domains)
        except (ValueError, BuildError) as e:
            flash(str(e), "error")
            return redirect(url_for("admin_review", request_id=request_id))
        db.update(request_id, status=new_status)
        flash(f"Approved. The widget now runs only on {', '.join(domains)} and your own machine.")
        return redirect(url_for("admin_delivery", request_id=request_id))
    elif action == "rebuild":
        # Clear the old results so the page doesn't show the previous agent while the new one builds.
        # The old agent and crawl folder stay in ElevenLabs; delete them there. Clearing folder_id
        # makes the rebuild crawl again, even if the first build reused a demo's crawl.
        db.update(request_id, status=new_status, progress="", error=None,
                  agent_id=None, folder_id=None, facts_doc_id=None, file_doc_ids=[])
        flash("Queued for a rebuild. Delete the old agent in the ElevenLabs dashboard, and its crawl folder "
              "unless a demo of the same site still uses it.")
    else:
        db.update(request_id, status=new_status)
        flash(f"Marked {new_status}.")
    return redirect(url_for("admin_review", request_id=request_id))


@app.post("/admin/requests/<int:request_id>/facts")
def admin_save_facts(request_id):
    found = get_or_404(request_id)
    facts = {key: request.form.get(key, "").strip() for key, _, _ in FACT_FIELDS if request.form.get(key, "").strip()}
    back = redirect(url_for("admin_review", request_id=request_id))

    if found["status"] == "building":
        flash("Wait for the build to finish before editing the facts.", "error")
        return back
    if found["facts_doc_id"]:
        text = facts_text(facts)
        if not text:
            flash("Fill in at least one field. This agent has a facts document, and it needs some text.", "error")
            return back
        # Update ElevenLabs first and the database second, so the database never claims
        # the agent has facts that ElevenLabs refused.
        try:
            update_facts(found["facts_doc_id"], text)
        except BuildError as e:
            flash(f"ElevenLabs didn't accept the change: {e}", "error")
            return back
        db.update(request_id, facts=facts)
        flash("Saved. The agent uses the new facts from its next conversation.")
    else:
        db.update(request_id, facts=facts)
        if found["status"] == "queued":
            flash("Saved. The build will use these facts.")
        else:
            flash("Saved. This agent was built without a facts document, so click Rebuild to use them.")
    return back
