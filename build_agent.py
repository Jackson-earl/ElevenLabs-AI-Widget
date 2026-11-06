"""Build an ElevenLabs support agent for a client company from its public website.

Usage:
    cp .env.example .env   # then put your API key in .env
    python build_agent.py --company "Acme Plumbing" --website https://acmeplumbing.com \
        [--notes intake.md] [--max-pages 200]

What it does:
    1. Crawls the company's website into a knowledge base folder (ElevenLabs does the crawling).
    2. Optionally uploads your own intake notes (hours, pricing, policies) as a text document.
    3. Creates an agent with RAG on, the folder and notes attached, and a greeting that
       asks the visitor what they need.
    4. Prints the embed snippet to give the client.

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
if not os.environ.get("ELEVENLABS_API_KEY"):
    sys.exit("ELEVENLABS_API_KEY is not set. Copy .env.example to .env and add your key.")

API = "https://api.elevenlabs.io/v1/convai"


def call(method, path, **kwargs):
    response = requests.request(
        method, f"{API}{path}", headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}, timeout=60, **kwargs
    )
    if not response.ok:
        sys.exit(f"{method} {path} failed ({response.status_code}): {response.text}")
    return response.json()


CRAWLER = {"User-Agent": "ElevenlabsBot/1.0"}
# Pages that add noise rather than answers.
EXCLUDE = ["*/cart*", "*/checkout*", "*/login*", "*/account*", "*/tag/*", "*/thank-you*", "*/feed", "*/.well-known/*"]


def check_site(website, force):
    """Fetch the homepage as the ElevenLabs crawler would. Returns the URL after redirects and
    the pages the homepage links to (its menu and footer: the pages that matter most).

    A blocked crawl still creates documents, just empty ones, so catch it before crawling."""
    try:
        home = requests.get(website, headers=CRAWLER, timeout=30)
    except requests.RequestException as e:
        sys.exit(f"Could not reach {website}: {e}")
    body = re.sub(r"(?s)<(script|style|noscript|svg)[^>]*>.*?</\1>", "", home.text)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
    problem = None
    if home.status_code in (401, 403, 429, 503) or "cf-chl" in home.text:
        problem = f"The site refused the ElevenLabs crawler (HTTP {home.status_code}). Ask the client to allow the ElevenlabsBot user agent."
    elif not home.ok:
        problem = f"The homepage returned HTTP {home.status_code}."
    elif len(text) < 300:
        problem = "The homepage has almost no text without JavaScript, so the crawler will save near-empty pages. Use --notes or upload the client's documents instead."
    if problem:
        if not force:
            sys.exit(problem + " Pass --force to crawl anyway.")
        print("Warning: " + problem)
    linked = {urljoin(home.url, href).split("#")[0].rstrip("/") for href in re.findall(r'href="([^"]+)"', home.text)}
    return home.url, linked


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


def crawl_website(website, linked, max_pages):
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
        print(f"Found {len(found)} pages in the sitemap and homepage menu, crawling {len(pages)}")
    else:
        print("No sitemap found, following links instead")
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
    print(f"Crawl job {job['id']} started for {website}")
    progress, last_change = None, time.time()
    while True:
        job = call("GET", f"/knowledge-base/crawl/{job['id']}")
        counts = tuple(job.get(k, 0) for k in ("pages_identified", "pages_scraped", "pages_skipped", "pages_failed"))
        if counts != progress:
            progress, last_change = counts, time.time()
            print(f"  {job['status']}: {counts[1]} of {counts[0]} scraped, {counts[2]} skipped, {counts[3]} failed")
        if job["status"] in ("succeeded", "failed", "skipped", "cancelled"):
            break
        # The last few pages of a crawl can sit for a long time. The pages already scraped are
        # usable, and the crawl keeps finishing the rest into the same folder in the background.
        if counts[1] and time.time() - last_change > 300:
            print("  No progress for 5 minutes; creating the agent now. Remaining pages will be added as they finish.")
            return job["root_folder_id"]
        time.sleep(10)
    if job["status"] != "succeeded" or job.get("pages_scraped", 0) == 0:
        sys.exit("Crawl did not produce any pages. Check that the site allows the ElevenLabs crawler.")
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    parser.add_argument("--website", required=True)
    parser.add_argument("--notes", help="Markdown or text file with facts the client gave you")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--force", action="store_true", help="Crawl even if the site looks blocked or empty")
    args = parser.parse_args()

    if "://" not in args.website:
        args.website = "https://" + args.website
    args.website, linked = check_site(args.website, args.force)
    knowledge_base = [
        {"type": "folder", "id": crawl_website(args.website, linked, args.max_pages), "name": f"{args.company} website", "usage_mode": "auto"}
    ]
    if args.notes:
        with open(args.notes) as f:
            doc = call("POST", "/knowledge-base/text", json={"text": f.read(), "name": f"{args.company} intake notes"})
        # Client-confirmed facts win over scraped pages, so keep them in the prompt on every turn.
        knowledge_base.append({"type": "text", "id": doc["id"], "name": doc["name"], "usage_mode": "prompt"})

    agent_id = create_agent(args.company, args.website, knowledge_base)
    print(f"\nAgent created: https://elevenlabs.io/app/agents/agents/{agent_id}")
    print("Before handing it over: add the client's domain to the agent's allowlist (Security tab).\n")
    print("Embed code for the client's site:\n")
    print(f'<elevenlabs-convai agent-id="{agent_id}"></elevenlabs-convai>')
    print('<script src="https://unpkg.com/@elevenlabs/convai-widget-embed" async type="text/javascript"></script>')


if __name__ == "__main__":
    main()
