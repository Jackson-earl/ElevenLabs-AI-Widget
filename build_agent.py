"""Build an ElevenLabs support agent for a client company from its public website.

Use it from the command line:
    cp .env.example .env   # then put your API key in .env
    python build_agent.py --company "Acme Plumbing" --website https://acmeplumbing.com \
        [--notes intake.md] [--file pricing.pdf] [--max-pages 200]

or from other Python code, such as the web app:
    from build_agent import build, BuildError
    result = build("Acme Plumbing", "https://acmeplumbing.com", facts="## Pricing\n...")

What it does:
    1. Crawls the company's website into a knowledge base folder (ElevenLabs does the crawling).
    2. Optionally uploads facts the client gave you (hours, pricing, policies) as a text document,
       and any files they sent (PDF, DOCX, TXT, MD) as documents of their own.
    3. Creates an agent with RAG on, everything above attached, and a greeting that
       asks the visitor what they need.

Requires: pip install requests python-dotenv
"""

import argparse
import gzip
import html
import os
import re
import sys
import time
from fnmatch import fnmatch
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

API = "https://api.elevenlabs.io/v1/convai"


class BuildError(Exception):
    """A build could not finish. The message is written for a person to read."""


def api_key():
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise BuildError("ELEVENLABS_API_KEY is not set. Copy .env.example to .env and add your key.")
    return key


def call(method, path, **kwargs):
    response = requests.request(method, f"{API}{path}", headers={"xi-api-key": api_key()}, timeout=60, **kwargs)
    if not response.ok:
        raise BuildError(f"{method} {path} failed ({response.status_code}): {response.text}")
    return response.json()


# File types ElevenLabs accepts, by extension. Each upload must say its type, or it is rejected.
FILE_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".epub": "application/epub+zip",
}
MAX_FILE_BYTES = 20 * 1024 * 1024

CRAWLER = {"User-Agent": "ElevenlabsBot/1.0"}
# Pages that add noise rather than answers.
EXCLUDE = ["*/cart*", "*/checkout*", "*/login*", "*/account*", "*/tag/*", "*/thank-you*", "*/feed", "*/.well-known/*"]


def check_site(website):
    """Fetch the homepage as the ElevenLabs crawler would. Returns three things: the URL after
    redirects, the pages the homepage links to (its menu and footer: the pages that matter most),
    and a problem message, or None if the site looks crawlable.

    A blocked crawl still creates documents, just empty ones, so catch it before crawling.
    The intake form calls this on its own to warn the client right away."""
    if "://" not in website:
        website = "https://" + website
    try:
        home = requests.get(website, headers=CRAWLER, timeout=30)
    except requests.RequestException:
        raise BuildError(f"Could not reach {website}. Check the address for typos.")
    body = re.sub(r"(?s)<(script|style|noscript|svg)[^>]*>.*?</\1>", "", home.text)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
    problem = None
    if home.status_code in (401, 403, 429, 503) or "cf-chl" in home.text:
        problem = f"The site refused the ElevenLabs crawler (HTTP {home.status_code}). Ask the client to allow the ElevenlabsBot user agent."
    elif not home.ok:
        problem = f"The homepage returned HTTP {home.status_code}."
    elif len(text) < 300:
        problem = "The homepage has almost no text without JavaScript, so the crawler will save near-empty pages. Add the business details or documents by hand instead."
    linked = {urljoin(home.url, href).split("#")[0].rstrip("/") for href in re.findall(r'href="([^"]+)"', home.text)}
    return home.url, linked, problem


def sitemap_pages(website):
    """Page URLs on this site from its sitemap(s), following sitemap indexes. Empty if there is none."""
    site = urlparse(website)
    root = f"{site.scheme}://{site.netloc}"
    try:
        robots = requests.get(f"{root}/robots.txt", headers=CRAWLER, timeout=30).text
    except requests.RequestException:
        robots = ""
    todo = re.findall(r"(?im)^sitemap:\s*(\S+)", robots) or [f"{root}/sitemap.xml"]
    pages, seen = [], set()
    # Huge stores list millions of pages across hundreds of files; a sample is enough to pick from.
    while todo and len(seen) < 50 and len(pages) < 20000:
        sitemap = todo.pop(0)
        if sitemap in seen:
            continue
        seen.add(sitemap)
        try:
            response = requests.get(sitemap, headers=CRAWLER, timeout=30)
            content = response.content
            if content[:2] == b"\x1f\x8b":  # .xml.gz served without gzip encoding
                content = gzip.decompress(content)
        except (requests.RequestException, OSError):
            continue
        text = content.decode("utf-8", "replace")
        if not response.ok or "<loc>" not in text:
            continue
        locs = [html.unescape(u.strip()) for u in re.findall(r"<loc>(.*?)</loc>", text, re.S)]
        if "<sitemapindex" in text:
            todo.extend(locs)
        else:
            pages.extend(locs)
    # Sitemaps can list other domains (a separate app or API site); keep this site only.
    host = site.netloc.removeprefix("www.")
    pages = [p for p in pages if urlparse(p).netloc.removeprefix("www.") == host]
    pages = [p for p in pages if not any(fnmatch(p, pattern) for pattern in EXCLUDE) and "?" not in p]
    # A section address such as https://site.com/help limits the crawl to that section.
    section = site.path.rstrip("/")
    if section:
        pages = [p for p in pages if (urlparse(p).path + "/").startswith(section + "/")]
    return list(dict.fromkeys(pages))


def priority(url, linked):
    """Sort key: pages the homepage links to first, then shallow pages (pricing, about) before
    deep ones (single posts, products), with translated copies (/fr/, /de-de/) last."""
    path = urlparse(url).path.rstrip("/")
    translated = bool(re.match(r"/[a-z]{2}([-_][a-z]{2})?(/|$)", path, re.I))
    return (url.rstrip("/") not in linked, translated, path.count("/"), len(path))


def crawl_website(website, linked, max_pages, on_progress):
    # Many site builders (Framer, Wix, Webflow) draw their menus with JavaScript, so a crawler
    # following links from the homepage finds only a few pages. The sitemap lists them all.
    found = sitemap_pages(website)
    if found:
        # On huge sites the menu pages can be missing from the part of the sitemap read above.
        site = urlparse(website)
        found += [
            url for url in linked
            if urlparse(url).netloc == site.netloc
            and (urlparse(url).path + "/").startswith(site.path.rstrip("/") + "/")
            and "?" not in url
            and not re.search(r"\.(css|js|mjs|json|xml|png|jpe?g|gif|svg|webp|ico|webmanifest|woff2?)$", url, re.I)
            and not any(fnmatch(url, pattern) for pattern in EXCLUDE)
        ]
        found = list(dict.fromkeys(u.rstrip("/") or u for u in found))
    pages = sorted(found, key=lambda url: priority(url, linked))[:max_pages]
    if pages:
        on_progress(f"Found {len(found)} pages in the sitemap and homepage menu, crawling {len(pages)}")
    else:
        on_progress("No sitemap found, following links instead")
    job = call(
        "POST",
        "/knowledge-base/crawl",
        json={
            "url": website,
            "max_pages": max_pages,
            **({"sitemap_urls": pages} if pages else {}),
            "exclude_patterns": EXCLUDE,
            # Re-crawl weekly and pick up new pages so the agent stays current.
            "enable_auto_sync": True,
            "auto_discover": True,
            "auto_remove": True,
        },
    )
    on_progress(f"Crawl job {job['id']} started for {website}")
    progress, last_change = None, time.time()
    while True:
        job = call("GET", f"/knowledge-base/crawl/{job['id']}")
        counts = tuple(job.get(k, 0) for k in ("pages_identified", "pages_scraped", "pages_skipped", "pages_failed"))
        if counts != progress:
            progress, last_change = counts, time.time()
            on_progress(f"{job['status']}: {counts[1]} of {counts[0]} scraped, {counts[2]} skipped, {counts[3]} failed")
        if job["status"] in ("succeeded", "failed", "skipped", "cancelled"):
            break
        # The last few pages of a crawl can sit for a long time. The pages already scraped are
        # usable, and the crawl keeps finishing the rest into the same folder in the background.
        if counts[1] and time.time() - last_change > 300:
            on_progress("No progress for 5 minutes; creating the agent now. Remaining pages will be added as they finish.")
            return job["root_folder_id"]
        time.sleep(10)
    if job["status"] != "succeeded" or job.get("pages_scraped", 0) == 0:
        raise BuildError("The crawl did not produce any pages. Check that the site allows the ElevenLabs crawler.")
    return job["root_folder_id"]


def system_prompt(company, website):
    return f"""You are the virtual assistant on {company}'s website ({website}).

# Goal
Help visitors get answers about {company}'s products, services, pricing, hours, and policies,
and route them to a human when needed.

# Opening
At the start, ask for the visitor's first name and what they need help with today.
If their question involves an order, booking, or account, also ask for their email.
Ask one question at a time. Do not insist if they decline.

# Rules
- Answer only from the knowledge base. If the answer is not there, say you are not sure
  and offer to connect them with the {company} team. Never invent prices, hours, or policies.
- Keep replies short and conversational.
- Do not discuss competitors or topics unrelated to {company}.
"""


def create_agent(company, website, knowledge_base):
    agent = call(
        "POST",
        "/agents/create",
        json={
            "name": f"{company} Assistant",
            "conversation_config": {
                "agent": {
                    "first_message": f"Hi! I'm the {company} assistant. What's your name, and what can I help you with today?",
                    "language": "en",
                    "prompt": {
                        "prompt": system_prompt(company, website),
                        "knowledge_base": knowledge_base,
                        "rag": {"enabled": True, "embedding_model": "e5_mistral_7b_instruct"},
                    },
                }
            },
            "platform_settings": {
                "widget": {"variant": "full", "placement": "bottom-right"},
                # Pull the visitor details the agent collected into each conversation's analysis.
                "data_collection": {
                    "visitor_name": {"type": "string", "description": "The visitor's first name, if given."},
                    "visitor_email": {"type": "string", "description": "The visitor's email, if given."},
                    "request_summary": {"type": "string", "description": "One sentence on what the visitor needed."},
                },
            },
        },
    )
    return agent["agent_id"]


# The business details the intake form asks for, as (key, label, hint). The form shows them;
# facts_text() turns the answers into the document the agent always has in its prompt.
FACT_FIELDS = [
    ("services", "What you sell or do", "Your main products or services, in a few lines"),
    ("pricing", "Pricing", "Prices, plans, or how you quote"),
    ("hours", "Hours and location", "Opening hours, address, or service area"),
    ("values", "Values and tone of voice", "How the assistant should sound, and what you stand for"),
    ("never_discuss", "Topics the assistant must never discuss", "Such as competitors, legal advice, or discounts"),
]


def facts_text(facts):
    """Turn form answers like {"pricing": "$49/month"} into a document with one section per
    answer. Empty answers are left out so the agent never sees a blank heading."""
    sections = [f"## {label}\n{facts[key].strip()}" for key, label, _ in FACT_FIELDS if facts.get(key, "").strip()]
    return "\n\n".join(sections)


def update_facts(facts_doc_id, text):
    """Replace the text of an agent's facts document. The agent uses the new text from its next
    conversation on, with no rebuild, because it refers to the document by ID."""
    call("PATCH", f"/knowledge-base/{facts_doc_id}", json={"content": text})


def embed_code(agent_id):
    return (
        f'<elevenlabs-convai agent-id="{agent_id}"></elevenlabs-convai>\n'
        '<script src="https://unpkg.com/@elevenlabs/convai-widget-embed" async type="text/javascript"></script>'
    )


def build(company, website, facts="", files=(), max_pages=200, force=False, on_progress=print):
    """Build one client's agent from start to finish and return the IDs to save.

    facts: text the client typed in (pricing, hours, values...). Empty means none.
    files: paths of files the client uploaded.
    force: build even if check_site found a problem.
    on_progress: called with a short message at each step. The command line prints them;
        the web app saves them so the admin page can show how far along a build is.

    Raises BuildError if anything goes wrong."""
    # Check everything that can fail fast before starting a crawl that takes minutes.
    api_key()
    for path in files:
        name = os.path.basename(path)
        if os.path.splitext(name)[1].lower() not in FILE_TYPES:
            raise BuildError(f"{name}: unsupported file type. Use one of: {', '.join(FILE_TYPES)}")
        if not os.path.isfile(path):
            raise BuildError(f"{name}: file not found")
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise BuildError(f"{name}: larger than the 20 MB limit")
    website, linked, problem = check_site(website)
    if problem and not force:
        raise BuildError(problem)
    if problem:
        on_progress("Warning: " + problem)

    folder_id = crawl_website(website, linked, max_pages, on_progress)
    knowledge_base = [{"type": "folder", "id": folder_id, "name": f"{company} website", "usage_mode": "auto"}]

    facts_doc_id = None
    if facts.strip():
        doc = call("POST", "/knowledge-base/text", json={"text": facts, "name": f"{company} facts"})
        facts_doc_id = doc["id"]
        # Client-confirmed facts win over scraped pages, so keep them in the prompt on every turn.
        knowledge_base.append({"type": "text", "id": doc["id"], "name": doc["name"], "usage_mode": "prompt"})

    file_doc_ids = []
    for path in files:
        name = os.path.basename(path)
        on_progress(f"Uploading {name}")
        file_type = FILE_TYPES[os.path.splitext(name)[1].lower()]
        with open(path, "rb") as f:
            doc = call("POST", "/knowledge-base/file", files={"file": (name, f, file_type)}, data={"name": f"{company}: {name}"})
        file_doc_ids.append(doc["id"])
        # Files can be long, so let the agent look up the relevant parts rather than read them all.
        knowledge_base.append({"type": "file", "id": doc["id"], "name": doc["name"], "usage_mode": "auto"})

    on_progress("Creating the agent")
    agent_id = create_agent(company, website, knowledge_base)
    on_progress("Done")
    return {"agent_id": agent_id, "folder_id": folder_id, "facts_doc_id": facts_doc_id, "file_doc_ids": file_doc_ids}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    parser.add_argument("--website", required=True)
    parser.add_argument("--notes", help="Markdown or text file with facts the client gave you")
    parser.add_argument("--file", action="append", default=[], help="A file to add; repeat for more")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--force", action="store_true", help="Crawl even if the site looks blocked or empty")
    args = parser.parse_args()

    facts = ""
    if args.notes:
        with open(args.notes) as f:
            facts = f.read()
    try:
        result = build(args.company, args.website, facts, args.file, args.max_pages, args.force)
    except BuildError as e:
        sys.exit(f"Error: {e}")

    print(f"\nAgent created: https://elevenlabs.io/app/agents/agents/{result['agent_id']}")
    print("Before handing it over: add the client's domain to the agent's allowlist (Security tab).\n")
    print("Embed code for the client's site:\n")
    print(embed_code(result["agent_id"]))


if __name__ == "__main__":
    main()
