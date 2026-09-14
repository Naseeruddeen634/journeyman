"""Ticket triage: route an incoming support ticket to the right queue."""

import json

from openai import OpenAI

client = OpenAI()

PROMPT = open("prompts/triage_prompt.txt").read()


def triage(ticket_text: str) -> dict:
    """Return {"queue": ..., "urgency": ..., "summary": ...} for one ticket."""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": f"Customer wrote: {ticket_text}. Classify it."},
        ],
    )
    return json.loads(response.choices[0].message.content)
