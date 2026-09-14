"""Summarise support tickets."""

import os

MODEL = os.environ.get("SUMMARY_MODEL", "gpt-4o-mini")


def _client():
    from openai import OpenAI
    return OpenAI(timeout=30.0, max_retries=3)


def summarise(text: str) -> str:
    """One-paragraph summary of a ticket."""
    r = _client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": "Summarise the ticket in one paragraph."},
                  {"role": "user", "content": text}],
    )
    return r.choices[0].message.content
