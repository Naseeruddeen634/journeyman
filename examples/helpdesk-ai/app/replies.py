"""Draft a reply to a customer, using the conversation so far."""

from openai import OpenAI

from app.context import fit

client = OpenAI()
MODEL = "gpt-4o-mini"


def draft_reply(history: list[dict]) -> str:
    """A reply draft for a human agent to review before sending."""
    messages = fit(history, budget=3000)
    response = client.chat.completions.create(model=MODEL, messages=messages, max_tokens=400)
    return response.choices[0].message.content
