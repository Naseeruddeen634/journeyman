"""Pull a contact out of an email."""

import json
import os
import re

MODEL = os.environ.get("EXTRACT_MODEL", "gpt-4o-mini")
FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def _client():
    from openai import OpenAI
    return OpenAI(timeout=30.0, max_retries=3)


def extract_contact(email_body: str) -> dict:
    """Return {"name": ..., "phone": ...} from an email."""
    response = _client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": "Reply with JSON: name, phone."},
                  {"role": "user", "content": email_body}],
        max_tokens=200,
    )
    raw = response.choices[0].message.content or ""
    m = FENCE.match(raw)
    cleaned = m.group(1) if m else raw
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model did not return JSON: {raw[:200]!r}") from exc
