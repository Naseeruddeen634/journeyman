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
BEDROCK_MODEL = os.environ.get("JOURNEYMAN_BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6")

# Ollama's default context window is 4096 tokens, and nothing here used to change
# it. Ollama's own log on the development machine showed prompts with a median of
# 1,817 tokens and a p90 of 2,832, and eight 'slot context shift' events with
# n_keep = 4 and n_discard = 2045: when the window filled mid-generation it kept
# the first four tokens and threw away the system prompt and the task. 16K costs
# roughly 1.2 GB more KV cache on Qwen3-Coder-30B-A3B.
LOCAL_CTX = int(os.environ.get("JOURNEYMAN_LOCAL_CTX", "16384"))
# Bounded output. Journeyman's reviewer flags an unbounded model call as AIE002;
# its own brains were exactly that.
MAX_OUTPUT_TOKENS = int(os.environ.get("JOURNEYMAN_MAX_OUTPUT_TOKENS", "4096"))
BEDROCK_REGION = os.environ.get("AWS_REGION", "us-west-2")
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
    bedrock_available: bool = False
    bedrock_model: str = BEDROCK_MODEL
    bedrock_region: str = BEDROCK_REGION
    detail: str = ""

    def summary(self) -> str:
        bits = [f"local {'ok' if self.local_available else 'MISSING'} ({self.local_model})",
                f"heavy {'ok' if self.heavy_available else 'off'}",
                f"bedrock {'ok' if self.bedrock_available else 'off'}"]
        return " | ".join(bits) + (f"  {self.detail}" if self.detail else "")


def local_brain(temperature: float = 0.2, **kw):
    """The everyday model. No network, no bill, no data leaving the machine."""
    from strands.models.ollama import OllamaModel

    return OllamaModel(
        host=OLLAMA_HOST,
        model_id=LOCAL_MODEL,
        temperature=temperature,
        max_tokens=MAX_OUTPUT_TOKENS,
        options={"num_ctx": LOCAL_CTX, **kw.pop("options", {})},
        # A shift is many calls, and a feedback check runs the test suite between
        # them, so keep the model loaded across those gaps. Not thirty minutes: on a
        # 24 GB laptop that is 18 GB held long after the shift has finished.
        keep_alive="10m",
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
        client_args={"api_key": key, "base_url": HEAVY_BASE_URL, "timeout": 120.0,
                     "max_retries": 3},
        model_id=HEAVY_MODEL,
        params={"temperature": temperature, "max_tokens": MAX_OUTPUT_TOKENS},
        **kw,
    )


def bedrock_brain(temperature: float = 0.2, **kw):
    """Claude on Amazon Bedrock.

    The right brain when the work is on AWS anyway: the credentials are already
    there, the traffic stays inside the account, and usage lands on the same
    bill as everything else. Requires model access to be granted in the region,
    which is a console step people forget.
    """
    from strands.models import BedrockModel

    return BedrockModel(
        model_id=BEDROCK_MODEL,
        region_name=BEDROCK_REGION,
        temperature=temperature,
        max_tokens=MAX_OUTPUT_TOKENS,
        **kw,
    )


def _bedrock_ready() -> tuple[bool, str]:
    """Credentials present and the region set. Does not spend a token to check."""
    try:
        import boto3
    except ImportError:
        return False, "boto3 not installed"
    try:
        session = boto3.Session()
        if session.get_credentials() is None:
            return False, "no AWS credentials found"
        region = session.region_name or os.environ.get("AWS_REGION")
        if not region:
            return False, "AWS credentials found but no region set"
        return True, ""
    except Exception as exc:
        return False, f"boto3 check failed: {type(exc).__name__}"


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

    bedrock_ok, bedrock_detail = _bedrock_ready()
    return BrainStatus(
        local_available=local_ok,
        local_model=LOCAL_MODEL,
        heavy_available=bool(os.environ.get(HEAVY_API_KEY_ENV)),
        heavy_model=HEAVY_MODEL,
        bedrock_available=bedrock_ok,
        bedrock_model=BEDROCK_MODEL,
        bedrock_region=BEDROCK_REGION,
        detail=detail or (bedrock_detail if not bedrock_ok else ""),
    )


def pick(task_difficulty: str = "routine"):
    """Choose a brain. Falls back to local rather than failing.

    Returns (model, name, why).
    """
    status = check()
    prefer = os.environ.get("JOURNEYMAN_PREFER", "local").lower()

    if prefer == "bedrock" and status.bedrock_available:
        return (bedrock_brain(), BEDROCK_MODEL,
                f"JOURNEYMAN_PREFER=bedrock, running in {status.bedrock_region}")
    if task_difficulty == "hard" and status.heavy_available:
        return heavy_brain(), HEAVY_MODEL, "task marked hard and the heavy brain is configured"
    if task_difficulty == "hard" and status.bedrock_available:
        return (bedrock_brain(), BEDROCK_MODEL,
                f"task marked hard, using Bedrock in {status.bedrock_region}")
    if status.local_available:
        why = "routine work" if task_difficulty != "hard" else (
            "task is hard but no heavy brain is configured, so the local model gets it"
        )
        return local_brain(), LOCAL_MODEL, why
    if status.bedrock_available:
        return bedrock_brain(), BEDROCK_MODEL, "no local model installed, falling back to Bedrock"
    if status.heavy_available:
        return heavy_brain(), HEAVY_MODEL, "no local model installed, falling back to the API"
    raise RuntimeError(
        "No brain available. Any one of: `ollama pull " + LOCAL_MODEL + "`, "
        "set " + HEAVY_API_KEY_ENV + ", or configure AWS credentials with Bedrock access."
    )
