"""Pull a contact out of an email."""

import json
import os

MODEL = os.environ.get("EXTRACT_MODEL", "gpt-4o-mini")


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
    return json.loads(response.choices[0].message.content)
