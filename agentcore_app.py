"""Journeyman's reviewer on Amazon Bedrock AgentCore Runtime.

    agentcore configure -e agentcore_app.py -r us-east-1 --requirements-file requirements-agentcore.txt
    agentcore launch
    agentcore invoke '{"repo": "https://github.com/owner/name", "mode": "explain"}'

The logic lives in journeyman/remote.py and is tested there; this file only
adapts it to the runtime.
"""

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from journeyman.remote import RequestError, handle

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload, context=None):
    try:
        return handle(payload or {})
    except RequestError as exc:
        # A bad request is an answer, not a crash: the caller sees why.
        return {"error": str(exc)}


if __name__ == "__main__":
    app.run()
