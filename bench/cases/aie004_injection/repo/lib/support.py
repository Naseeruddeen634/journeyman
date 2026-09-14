"""Customer support reply drafting."""


def build_prompt(ticket_text: str, tone: str = "warm") -> str:
    """Build the prompt sent to the model for one ticket."""
    prompt = f"You are a support agent. Tone: {tone}. Reply to: {ticket_text}"
    return prompt
