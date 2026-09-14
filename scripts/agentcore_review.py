"""Send a local repository to the Journeyman reviewer running on AgentCore, print the answer.

    python scripts/agentcore_review.py ~/code/helpdesk-ai --arn <agent runtime ARN> [--mode review]

Only the repository's committed HEAD is sent (git archive), never uncommitted files.
Credentials come from the normal AWS chain (aws configure, SSO, or a role).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from journeyman.remote import pack  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--arn", required=True, help="the agent runtime ARN printed by agentcore launch")
    ap.add_argument("--mode", choices=["review", "explain"], default="explain")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--region", default=None)
    a = ap.parse_args()

    import boto3

    repo = Path(a.repo).expanduser().resolve()
    payload = {"archive": pack(repo), "name": repo.name, "mode": a.mode, "prompt": a.prompt}
    client = boto3.client("bedrock-agentcore", region_name=a.region)
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=a.arn, qualifier="DEFAULT",
        runtimeSessionId=f"journeyman-{uuid.uuid4()}",          # at least 33 characters
        contentType="application/json", accept="application/json",
        payload=json.dumps(payload).encode())
    body = resp["response"].read().decode()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print(body)
        return 0
    if "error" in data:
        print(f"\n  error: {data['error']}\n", file=sys.stderr)
        return 1
    print(f"\n  {data['repo']}: {len(data['findings'])} finding(s), reviewed on AgentCore\n")
    for f in data["findings"][:10]:
        print(f"  [{f['severity']:>2}] {f['code']}  {f['title']}\n        {f['where']}")
    if data.get("explanation"):
        print("\n  " + data["explanation"].replace("\n", "\n  ") + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
