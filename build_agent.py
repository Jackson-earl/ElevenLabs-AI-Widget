"""Build an ElevenLabs support agent for a client company from its public website.

Usage:
    export ELEVENLABS_API_KEY=...
    python build_agent.py --company "Acme Plumbing" --website https://acmeplumbing.com \
        [--notes intake.md] [--max-pages 200]

What it does:
    1. Crawls the company's website into a knowledge base folder (ElevenLabs does the crawling).
    2. Optionally uploads your own intake notes (hours, pricing, policies) as a text document.
    3. Creates an agent with RAG on, the folder and notes attached, and a greeting that
       asks the visitor what they need.
    4. Prints the embed snippet to give the client.

Requires: pip install requests
"""

import argparse
import os
import sys
import time

import requests

API = "https://api.elevenlabs.io/v1/convai"


def call(method, path, **kwargs):
    response = requests.request(
        method, f"{API}{path}", headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}, timeout=60, **kwargs
    )
    if not response.ok:
        sys.exit(f"{method} {path} failed ({response.status_code}): {response.text}")
    return response.json()


def crawl_website(website, max_pages):
    job = call(
        "POST",
        "/knowledge-base/crawl",
        json={
            "url": website,
            "max_pages": max_pages,
            # Skip pages that add noise rather than answers.
            "exclude_patterns": ["*/cart*", "*/checkout*", "*/login*", "*/account*", "*/tag/*"],
            # Re-crawl weekly and pick up new pages so the agent stays current.
            "enable_auto_sync": True,
            "auto_discover": True,
            "auto_remove": True,
        },
    )
    print(f"Crawl job {job['id']} started for {website}")
    while True:
        job = call("GET", f"/knowledge-base/crawl/{job['id']}")
        print(f"  {job['status']}: {job.get('pages_scraped', 0)} scraped, {job.get('pages_failed', 0)} failed")
        if job["status"] in ("succeeded", "failed", "skipped", "cancelled"):
            break
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
    args = parser.parse_args()

    knowledge_base = [
        {"type": "folder", "id": crawl_website(args.website, args.max_pages), "name": f"{args.company} website", "usage_mode": "auto"}
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
