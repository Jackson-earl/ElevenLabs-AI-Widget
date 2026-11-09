"""The web app: the intake form clients fill in, and the admin pages where you review agents.

Run it with:
    flask --app app run --debug --port 5001
then open http://127.0.0.1:5001 (intake form) or http://127.0.0.1:5001/admin (admin pages).

(Port 5000 is taken by AirPlay Receiver on most Macs, hence 5001.)

Requires: pip install flask
"""

import hmac
import os
import shutil
import time
import uuid

from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

import db
from build_agent import FACT_FIELDS, FILE_TYPES, MAX_FILE_BYTES, BuildError, check_site, facts_text, update_facts

UPLOADS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
MAX_FILES = 5

app = Flask(__name__)
# Flask rejects any submission bigger than this before our code runs.
app.config["MAX_CONTENT_LENGTH"] = MAX_FILES * MAX_FILE_BYTES + 1024 * 1024
# flash() keeps its messages in a signed cookie, which needs a secret key. A random one is fine
# for testing; set SECRET_KEY in .env once the app runs on a real server.
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)


@app.get("/")
def intake():
    return render_template("intake.html", fields=FACT_FIELDS, form={}, error=None)


@app.post("/")
def submit():
    form = request.form

    def show_error(message):
        # Show the form again with the client's answers still filled in.
        return render_template("intake.html", fields=FACT_FIELDS, form=form, error=message), 400

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
    db.add_request(company, website, email, facts=facts, files=paths)
    # Redirect after a successful POST, so refreshing the thanks page cannot submit twice.
    return redirect(url_for("thanks"))


@app.get("/thanks")
def thanks():
    return render_template("thanks.html")


@app.errorhandler(413)
def too_large(_):
    return render_template("intake.html", fields=FACT_FIELDS, form={},
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


# Which statuses each action is allowed from. For example, a request being built can't be
# rebuilt or approved until the worker has finished with it.
ACTIONS = {
    "approve": ({"ready_for_review"}, "approved"),
    "reject": ({"ready_for_review", "failed"}, "rejected"),
    "rebuild": ({"ready_for_review", "failed", "rejected"}, "queued"),
}


def get_or_404(request_id):
    found = db.get_request(request_id)
    if found is None:
        abort(404)
    return found


@app.get("/admin")
def admin_list():
    rows = db.list_requests()
    busy = any(r["status"] in ("queued", "building") for r in rows)
    return render_template("admin_list.html", rows=rows, refresh=busy)


@app.get("/admin/requests/<int:request_id>")
def admin_review(request_id):
    found = get_or_404(request_id)
    files = [os.path.basename(path) for path in found["files"]]
    return render_template("admin_review.html", r=found, files=files, fields=FACT_FIELDS,
                           actions=[name for name, (allowed, _) in ACTIONS.items() if found["status"] in allowed],
                           refresh=found["status"] in ("queued", "building"))


@app.post("/admin/requests/<int:request_id>/<action>")
def admin_action(request_id, action):
    if action not in ACTIONS:
        abort(404)
    found = get_or_404(request_id)
    allowed, new_status = ACTIONS[action]
    if found["status"] not in allowed:
        flash(f"Can't {action} a request that is {found['status'].replace('_', ' ')}.", "error")
    elif action == "rebuild":
        # Clear the old results so the page doesn't show the previous agent while the new one builds.
        # The old agent and crawl folder stay in ElevenLabs; delete them there.
        db.update(request_id, status=new_status, progress="", error=None,
                  agent_id=None, folder_id=None, facts_doc_id=None, file_doc_ids=[])
        flash("Queued for a rebuild. Delete the old agent and crawl folder in the ElevenLabs dashboard.")
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
