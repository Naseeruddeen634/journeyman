"""Route tickets to a queue."""


def _client():
    from openai import OpenAI
    return OpenAI(timeout=30.0, max_retries=3)


def classify(text: str) -> str:
    """Return billing, technical or account."""
    r = _client().chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "Answer billing, technical or account."},
                  {"role": "user", "content": text}],
        max_tokens=5,
    )
    return r.choices[0].message.content.strip()
