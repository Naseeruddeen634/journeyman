"""The model inventory: which calls, which models, where the text goes."""

import json
import textwrap
from pathlib import Path

from journeyman.inventory import build, python_sites, render, typescript_sites


def sites(src: str, rel: str = "app/llm.py"):
    return python_sites(rel, textwrap.dedent(src))


def one(src: str, rel: str = "app/llm.py"):
    found = sites(src, rel)
    assert len(found) == 1, found
    return found[0]


def test_openai_literal_model_goes_to_openai():
    s = one("""
        from openai import OpenAI
        client = OpenAI()
        def ask(q):
            return client.chat.completions.create(model="gpt-4o-mini", max_tokens=200,
                                                  messages=[{"role": "user", "content": q}])
    """)
    assert (s.kind, s.provider, s.model, s.model_source) == \
        ("chat.completions.create", "openai-compatible", "gpt-4o-mini", "literal")
    assert s.destination == "api.openai.com" and s.leaves_machine is True and s.output_limit is True


def test_env_constant_is_resolved_with_its_default():
    s = one("""
        import os
        from openai import OpenAI
        MODEL = os.environ.get("SUMMARY_MODEL", "gpt-4o-mini")
        def run(client, text):
            return client.chat.completions.create(model=MODEL, messages=[])
    """)
    assert s.model == "env SUMMARY_MODEL (default gpt-4o-mini)" and s.model_source == "env"
    assert s.output_limit is False


def test_plain_constant_and_dynamic_model_are_labelled_not_guessed():
    found = sites("""
        from anthropic import Anthropic
        DEFAULT = "claude-sonnet-4-6"
        def a(c): return c.messages.create(model=DEFAULT, max_tokens=10, messages=[])
        def b(c, cfg): return c.messages.create(model=cfg.model, max_tokens=10, messages=[])
    """)
    assert [s.model_source for s in found] == ["constant", "dynamic"]
    assert found[0].model == "claude-sonnet-4-6 (constant DEFAULT)"
    assert found[1].model == "dynamic: cfg.model"
    assert all(s.destination == "api.anthropic.com" for s in found)


def test_twilio_messages_create_is_not_a_model_call():
    assert sites("""
        from twilio.rest import Client
        from openai import OpenAI   # an LLM file, so the gate is open
        def notify(c, body):
            c.messages.create(to="+100", from_="+200", body=body)
    """) == []


def test_openai_client_pointed_at_ollama_stays_local():
    s = one("""
        from openai import OpenAI
        client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
        def ask(q):
            return client.chat.completions.create(model="qwen3-coder:30b", messages=[], max_tokens=5)
    """)
    assert s.leaves_machine is False and s.destination.startswith("local (")


def test_base_url_from_configuration_is_unknown_not_openai():
    s = one("""
        import os
        from openai import OpenAI
        client = OpenAI(base_url=os.environ["LLM_BASE_URL"])
        def ask(q):
            return client.chat.completions.create(model="m-1", messages=[], max_tokens=5)
    """)
    assert s.leaves_machine is None and "not visible" in s.destination


def test_framework_constructors_and_env_host():
    found = sites("""
        import os
        from strands.models.ollama import OllamaModel
        from strands.models import BedrockModel
        HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        local = OllamaModel(host=HOST, model_id="qwen3-coder:30b", max_tokens=4096)
        cloud = BedrockModel(model_id=os.getenv("BEDROCK_MODEL", "anthropic.claude"), **extra)
    """)
    ollama, bedrock = found
    assert ollama.destination == "local (ollama), unless OLLAMA_HOST is set" and ollama.leaves_machine is False
    assert bedrock.provider == "bedrock" and bedrock.output_limit is None      # **extra may hold it
    assert bedrock.model == "env BEDROCK_MODEL (default anthropic.claude)"


def test_bedrock_converse_limit_inside_inference_config_and_opaque_body():
    found = sites("""
        import boto3, json
        brt = boto3.client("bedrock-runtime")
        def a(msgs):
            return brt.converse(modelId="anthropic.claude-3-haiku", messages=msgs,
                                inferenceConfig={"maxTokens": 300})
        def b(body):
            return brt.invoke_model(modelId="anthropic.claude-3-haiku", body=body)
        def c(msgs):
            return brt.converse(modelId="anthropic.claude-3-haiku", messages=msgs)
    """)
    assert [s.output_limit for s in found] == [True, None, False]
    assert all(s.destination == "AWS account (bedrock)" for s in found)


def test_strands_agent_without_a_model_uses_bedrock():
    found = sites("""
        from strands import Agent
        from strands.models.ollama import OllamaModel
        m = OllamaModel(host="http://localhost:11434", model_id="llama3.2", max_tokens=100)
        explicit = Agent(model=m)
        default = Agent(system_prompt="You are a helpful assistant")
    """)
    kinds = [(s.kind, s.provider) for s in found]
    assert ("OllamaModel", "ollama") in kinds
    assert ("Agent (strands default model)", "bedrock") in kinds and len(found) == 2


def test_calls_in_tests_are_marked():
    s = one("""
        from openai import OpenAI
        def test_live(): OpenAI().chat.completions.create(model="gpt-4o", messages=[])
    """, rel="tests/test_live.py")
    assert s.test


def test_typescript_openai_sdk_and_vercel_ai_sdk():
    src = textwrap.dedent("""
        import OpenAI from "openai";
        import { generateText } from "ai";
        import { anthropic } from "@ai-sdk/anthropic";
        const client = new OpenAI();
        // client.chat.completions.create({ model: "in-a-comment" })
        export async function a(q: string) {
          return client.chat.completions.create({ model: process.env.APP_MODEL ?? "gpt-4o-mini",
            messages: [{ role: "user", content: q }] });
        }
        export async function b(prompt: string) {
          return generateText({ model: anthropic("claude-sonnet-4-6"), prompt, maxOutputTokens: 400 });
        }
    """)
    found = typescript_sites("src/llm.ts", src)
    assert len(found) == 2
    a, b = found
    assert a.model == "env APP_MODEL (default gpt-4o-mini)" and a.output_limit is False
    assert (b.kind, b.provider, b.model, b.output_limit) == \
        ("generateText", "anthropic", "claude-sonnet-4-6", True)


def test_build_renders_and_reports_prompt_eval_status(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "llm.py").write_text(textwrap.dedent("""
        from openai import OpenAI
        def ask(c, q): return c.chat.completions.create(model="gpt-4o", messages=[])
    """))
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "triage.txt").write_text("You are a triage assistant. {ticket}")
    (tmp_path / "prompts" / "reply_prompt.md").write_text("You are a support agent.")
    ev = tmp_path / "evals" / "triage"
    ev.mkdir(parents=True)
    (ev / "cases.jsonl").write_text('{"id": "a"}\n{"id": "b"}\n')
    (ev / "baseline.json").write_text(json.dumps({"scores": {"holdout": 0.75}}))

    inv = build(tmp_path)
    assert [p.eval_status for p in inv.prompts] == ["no eval", "eval: 2 cases, holdout baseline 75%"]
    text = render(inv)
    assert "1 call site(s) in 1 file(s)" in text and "NO LIMIT" in text
    assert "1 with the model id hardcoded" in text
    assert json.loads(json.dumps(inv.to_dict()))["sites"][0]["model"] == "gpt-4o"


def test_journeymans_own_brains_are_inventoried_accurately():
    repo = Path(__file__).resolve().parents[1]
    prod = build(repo).production()
    by_kind = {s.kind: s for s in prod if s.where.startswith("journeyman/brain/")}
    assert set(by_kind) == {"OllamaModel", "OpenAIModel", "BedrockModel"}
    assert by_kind["OllamaModel"].leaves_machine is False
    assert by_kind["OpenAIModel"].destination.startswith("openrouter.ai")
    assert all(s.output_limit for s in by_kind.values())
