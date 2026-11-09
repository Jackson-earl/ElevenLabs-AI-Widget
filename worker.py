"""Builds agents for queued requests, one at a time.

Run it in its own terminal, next to the web app:
    python worker.py

It checks the database every few seconds. Stop it with Ctrl+C.
"""

import logging
import time
import traceback

import db
from build_agent import BuildError, build, facts_text

POLL_SECONDS = 5

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


def process(request):
    request_id = request["id"]
    log.info("Building request %s: %s (%s)", request_id, request["company"], request["website"])

    def on_progress(message):
        log.info("  [%s] %s", request_id, message)
        db.update(request_id, progress=message)

    try:
        result = build(
            request["company"],
            request["website"],
            facts=facts_text(request["facts"]),
            files=request["files"],
            on_progress=on_progress,
        )
    except BuildError as e:
        # An expected problem, such as a blocked site or a rejected file. The message is readable as-is.
        db.update(request_id, status="failed", error=str(e))
        log.info("Request %s failed: %s", request_id, e)
        return
    except Exception:
        # A bug or a network outage. Save the full traceback so you can debug it, and keep running.
        db.update(request_id, status="failed", error="Unexpected error:\n" + traceback.format_exc())
        log.exception("Request %s crashed", request_id)
        return

    db.update(
        request_id,
        status="ready_for_review",
        error=None,
        agent_id=result["agent_id"],
        folder_id=result["folder_id"],
        facts_doc_id=result["facts_doc_id"],
        file_doc_ids=result["file_doc_ids"],
    )
    log.info("Request %s is ready for review (agent %s)", request_id, result["agent_id"])


def main():
    recover_interrupted()
    log.info("Worker started; checking for requests every %s seconds", POLL_SECONDS)
    while True:
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
