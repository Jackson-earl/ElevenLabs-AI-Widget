"""Writes the sales playbook for an industry, with OpenAI, as a draft for you to approve.

The playbook is built around the five questions every prospect asks before buying (can you help
me, do you serve my area, what will it cost, how soon can we start, why should I choose you),
phrased the way customers in that industry ask them. It also lists other common questions, the
details to collect so the lead is ready for a salesperson, and what to hand to a person.

Every agent in an industry shares one approved copy, so it is written once per industry, not
once per client. Each client's own answers come from their intake form. Set these in .env:
    OPENAI_API_KEY=sk-...
    OPENAI_MODEL=gpt-6.1-sol    optional; this is the default
"""

import json
import os

import requests

# The five questions from the business plan, in the order the playbook lists them.
PROSPECT_QUESTIONS = {
    "can_you_help": "Can you help me?",
    "service_area": "Do you serve my area?",
    "cost": "What will it cost?",
    "timing": "How soon can we start?",
    "why_choose": "Why should I choose you?",
}

MORE_QUESTIONS = 5

INSTRUCTIONS = f"""You write the sales playbook for AI assistants on small business websites. The assistant
answers a visitor's questions, shows why the business is a good choice, and gathers what a salesperson
needs to follow up.

For the industry the user names, write:

1. The five questions every prospect asks before buying, phrased the way customers in this industry ask
   them. For "What will it cost?" a heating and air customer asks about the diagnostic fee, estimates,
   and financing; a law firm's client asks about consultation fees and how the firm charges. For each:
   - question: the most common way customers in this industry ask it.
   - other_phrasings: two or three other ways they ask it, in their own words.
   - answer: what an experienced, honest expert in this industry would tell them, in two to four plain
     sentences that suit being read aloud.
   - company_details: the specific facts a business must provide to finish the answer for itself, such
     as "diagnostic fee", "ZIP codes served", or "license number".
   - next_step: what the assistant offers next to move toward a sale, such as an estimate, a visit,
     a consultation, or a callback.
2. other_questions: the {MORE_QUESTIONS} other questions customers in this industry ask most, with answers
   written the same way.
3. lead_details: what a salesperson in this industry needs to know before calling a prospect back,
   beyond their name, contact details, location, urgency, and preferred callback time. For example,
   the type and age of the system, or the type of legal matter and any deadlines.
4. handoffs: what the assistant must never handle itself and must pass to a person, including any
   safety situations, with what to tell the visitor. For example, a law firm's assistant gives no legal
   advice, and attorneys decide conflict checks and whether to take a case.

Everything is shared by every business in the industry, so:
- Never name a company, brand, place, or law of one region, and never speak as a particular business.
- Never give a price, time, or policy as a fact. Give typical ranges and say they vary by business,
  location, and job; the company's own details come from its company_details.
- Leave out anything you are not confident is true."""

QA = {
    "type": "object",
    "properties": {"question": {"type": "string"}, "answer": {"type": "string"}},
    "required": ["question", "answer"],
    "additionalProperties": False,
}
STRINGS = {"type": "array", "items": {"type": "string"}}
PROSPECT = {
    "type": "object",
    "properties": {"question": {"type": "string"}, "other_phrasings": STRINGS, "answer": {"type": "string"},
                   "company_details": STRINGS, "next_step": {"type": "string"}},
    "required": ["question", "other_phrasings", "answer", "company_details", "next_step"],
    "additionalProperties": False,
}
# Each prospect question has its own property, so the reply always covers all five.
SCHEMA = {
    "type": "object",
    "properties": {
        "prospect_questions": {"type": "object", "properties": {key: PROSPECT for key in PROSPECT_QUESTIONS},
                               "required": list(PROSPECT_QUESTIONS), "additionalProperties": False},
        "other_questions": {"type": "array", "items": QA},
        "lead_details": STRINGS,
        "handoffs": STRINGS,
    },
    "required": ["prospect_questions", "other_questions", "lead_details", "handoffs"],
    "additionalProperties": False,
}


class IndustryError(Exception):
    """The playbook could not be written. The message is written for a person to read."""


def bullets(items):
    return "\n".join(f"- {item.strip()}" for item in items if item.strip())


def to_markdown(playbook):
    """The playbook as the Markdown you review and edit on the admin page. The agent reads the same
    text, and the system prompt refers to its four "# " sections by name."""
    sections = ["# Questions prospects ask"]
    for key, topic in PROSPECT_QUESTIONS.items():
        p = playbook["prospect_questions"][key]
        lines = [f"## {topic} {p['question'].strip()}"]
        if p["other_phrasings"]:
            lines.append("Customers also ask: " + "; ".join(q.strip() for q in p["other_phrasings"] if q.strip()))
        lines.append(p["answer"].strip())
        if p["company_details"]:
            lines.append("Company details to use: " + ", ".join(d.strip() for d in p["company_details"] if d.strip()))
        lines.append("Next step: " + p["next_step"].strip())
        sections.append("\n".join(lines))
    sections.append("# Other common questions")
    sections += [f"## {q['question'].strip()}\n{q['answer'].strip()}" for q in playbook["other_questions"]
                 if q["question"].strip()]
    sections.append("# Details to gather for the salesperson\n" + bullets(playbook["lead_details"]))
    sections.append("# Hand these to a person\n" + bullets(playbook["handoffs"]))
    return "\n\n".join(sections)


def write_questions(industry):
    """The sales playbook for an industry, as Markdown."""
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
                                    "json_schema": {"name": "industry_playbook", "strict": True, "schema": SCHEMA}},
            },
            timeout=180,
        )
    except requests.RequestException as e:
        raise IndustryError(f"Couldn't reach OpenAI ({e}).")
    if not response.ok:
        raise IndustryError(f"OpenAI refused the request ({response.status_code}): {response.text}")
    message = response.json()["choices"][0]["message"]
    if message.get("refusal"):
        raise IndustryError(f"OpenAI declined to write this playbook: {message['refusal']}")
    try:  # a reply cut short by the token limit isn't valid JSON
        return to_markdown(json.loads(message["content"]))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise IndustryError("OpenAI's reply wasn't in the expected format. Click Write again.")
