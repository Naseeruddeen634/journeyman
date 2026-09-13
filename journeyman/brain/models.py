"""Which model does the thinking.

Two brains, because one is not enough and three is fuss.

  local      Qwen3-Coder 30B-A3B through Ollama. Runs on the laptop, costs
             nothing, works on a plane, and never sends a line of your
             employer's source anywhere. Does the routine work, which is most
             of the work.

  heavy      Kimi K3 through an OpenAI-compatible endpoint. 2.8T parameters and
             a million-token context, which is the right tool when the local
             model is stuck or the task needs the whole repository in view.
             It is called deliberately and it is metered.

Kimi K3 cannot be run locally. At MXFP4 the weights are on the order of a
terabyte; this machine has 24GB. Anyone who tells you otherwise has not done
the arithmetic. Using it through an API is still using an open-weight model:
you can audit it, self-host it on real hardware, and move providers.

Escalation is explicit rather than automatic-and-invisible. The agent asks for
the heavy brain when it has a reason, and the reason is written to the log.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


LOCAL_MODEL = os.environ.get("JOURNEYMAN_LOCAL_MODEL", "qwen3-coder:30b")
HEAVY_MODEL = os.environ.get("JOURNEYMAN_HEAVY_MODEL", "moonshotai/kimi-k3")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
HEAVY_BASE_URL = os.environ.get("JOURNEYMAN_HEAVY_BASE_URL", "https://openrouter.ai/api/v1")
HEAVY_API_KEY_ENV = "OPENROUTER_API_KEY"


@dataclass
class BrainStatus:
    local_available: bool
    local_model: str
    heavy_available: bool
    heavy_model: str
    detail: str = ""

    def summary(self) -> str:
        bits = [f"local {'ok' if self.local_available else 'MISSING'} ({self.local_model})",
                f"heavy {'ok' if self.heavy_available else 'not configured'} ({self.heavy_model})"]
        return " | ".join(bits) + (f"  {self.detail}" if self.detail else "")


def local_brain(temperature: float = 0.2, **kw):
    """The everyday model. No network, no bill, no data leaving the machine."""
    from strands.models.ollama import OllamaModel

    return OllamaModel(
        host=OLLAMA_HOST,
        model_id=LOCAL_MODEL,
        temperature=temperature,
        **kw,
    )


def heavy_brain(temperature: float = 0.3, **kw):
    """Kimi K3. Call it when the local model is out of its depth, not by default."""
    from strands.models.openai import OpenAIModel

    key = os.environ.get(HEAVY_API_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"{HEAVY_API_KEY_ENV} is not set, so the heavy brain is unavailable. "
            f"The agent will keep working on {LOCAL_MODEL} alone."
        )
    return OpenAIModel(
        client_args={"api_key": key, "base_url": HEAVY_BASE_URL},
        model_id=HEAVY_MODEL,
        params={"temperature": temperature},
        **kw,
    )


def check() -> BrainStatus:
    """Preflight. Called before the agent is allowed to run unattended."""
    local_ok, detail = False, ""
    try:
        import ollama

        client = ollama.Client(host=OLLAMA_HOST)
        names = {m.get("model") or m.get("name") for m in client.list().get("models", [])}
        local_ok = any(n and n.startswith(LOCAL_MODEL.split(":")[0]) for n in names)
        if not local_ok:
            detail = f"run: ollama pull {LOCAL_MODEL}"
    except Exception as exc:
        detail = f"ollama unreachable at {OLLAMA_HOST}: {type(exc).__name__}"

    return BrainStatus(
        local_available=local_ok,
        local_model=LOCAL_MODEL,
        heavy_available=bool(os.environ.get(HEAVY_API_KEY_ENV)),
        heavy_model=HEAVY_MODEL,
        detail=detail,
    )


def pick(task_difficulty: str = "routine"):
    """Choose a brain. Falls back to local rather than failing.

    Returns (model, name, why).
    """
    status = check()
    if task_difficulty == "hard" and status.heavy_available:
        return heavy_brain(), HEAVY_MODEL, "task marked hard and the heavy brain is configured"
    if status.local_available:
        why = "routine work" if task_difficulty != "hard" else (
            "task is hard but no heavy brain is configured, so the local model gets it"
        )
        return local_brain(), LOCAL_MODEL, why
    if status.heavy_available:
        return heavy_brain(), HEAVY_MODEL, "no local model installed, falling back to the API"
    raise RuntimeError(
        "No brain available. Either `ollama pull " + LOCAL_MODEL + "` or set "
        + HEAVY_API_KEY_ENV + "."
    )
