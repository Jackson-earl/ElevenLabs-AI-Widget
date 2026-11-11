"""Writes the common customer questions for an industry, with OpenAI, as a draft for you to approve.

Every agent in an industry shares one approved copy, so the questions are written once per
industry, not once per client. Set these in .env:
    OPENAI_API_KEY=sk-...
    OPENAI_MODEL=gpt-6.1-sol    optional; this is the default
"""

import json
import os

import requests

QUESTION_COUNT = 10

INSTRUCTIONS = f"""You write the knowledge base for AI assistants on small business websites.

List the {QUESTION_COUNT} questions that customers most often ask a business in the industry the user
names, before or while buying from it: what a service involves, how long it takes, what affects the
price, how to choose a provider, what to expect, what to prepare, warranties, and so on.

Answer each one the way an experienced, honest expert in that industry would explain it to a customer,
in two to five plain sentences that suit being read aloud.

The answers are general background shared by every business in the industry, so:
- Never name a company, brand, place, or law of one region, and never speak as a particular business.
- Never give a price, time, or policy as a fact. Give typical ranges and say they vary by business,
  location, and job.
- Where the answer depends on the business, say what to ask about, so the assistant can follow it
  with that business's own details.
- Leave out anything you are not confident is true."""

SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "answer": {"type": "string"}},
                "required": ["question", "answer"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


class IndustryError(Exception):
    """The questions could not be written. The message is written for a person to read."""


def write_questions(industry):
    """The common questions and answers for an industry, as Markdown with one "## question"
    heading per question. That is the text you review and edit in the admin page."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise IndustryError("OPENAI_API_KEY is not set. Add it to .env, then click Write again.")
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": os.environ.get("OPENAI_MODEL", "gpt-6.1-sol"),
                "messages": [
                    {"role": "system", "content": INSTRUCTIONS},
                    {"role": "user", "content": f"Industry: {industry}"},
                ],
                # Structured Outputs: the reply is always JSON that matches SCHEMA.
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "industry_questions", "strict": True, "schema": SCHEMA}},
            },
            timeout=180,
        )
    except requests.RequestException as e:
        raise IndustryError(f"Couldn't reach OpenAI ({e}).")
    if not response.ok:
        raise IndustryError(f"OpenAI refused the request ({response.status_code}): {response.text}")
    message = response.json()["choices"][0]["message"]
    if message.get("refusal"):
        raise IndustryError(f"OpenAI declined to write these questions: {message['refusal']}")
    try:  # a reply cut short by the token limit isn't valid JSON
        pairs = json.loads(message["content"])["questions"]
    except (ValueError, KeyError, TypeError):
        raise IndustryError("OpenAI's reply wasn't in the expected format. Click Write again.")
    return "\n\n".join(f"## {p['question'].strip()}\n{p['answer'].strip()}" for p in pairs if p["question"].strip())
