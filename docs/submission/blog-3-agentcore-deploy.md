# #Agents for Humans: putting an AI code reviewer on Amazon Bedrock AgentCore in an afternoon

Journeyman started as an agent that lives on one laptop: it reviews LLM code the way a senior AI
engineer would, and fixes things overnight in an isolated git worktree. That helps one person. A team
wants the same judgement from CI, from a chat bot, or from a teammate with no local model. This is how
I put the reviewer on Amazon Bedrock AgentCore Runtime with the Strands Agents SDK, what went wrong
on the way, and what it cost.

## Deciding what the model is allowed to do

The design question came before any AWS console. A code reviewer that runs as a service will be sent
code by people I do not know. So:

- **Findings are deterministic.** The service runs the same checks as `journeyman review`: a
  hardcoded model id, no `max_tokens`, `json.loads` on model output, no timeout, a prompt with no eval.
  No model decides what is wrong, so the verdict does not change between two calls.
- **The model only explains.** A Strands `Agent` with Claude on Amazon Bedrock gets the findings, and
  one tool, `read_file`, which can only read inside the extracted repository. It explains the top
  problems and writes the smallest fix.
- **Nothing from the repository is executed.** It is unpacked, read and deleted. Running untrusted
  tests is the job of the local `shift`, which has an OS sandbox.
- **It can only fetch `https://github.com/owner/name`.** A payload can also carry a tar.gz of a local
  repository's committed HEAD, so a private or unpushed repo can be reviewed without the service
  getting access to where it lives. Archives are extracted with Python's `data` filter, so a path like
  `../../escape` is refused. There is a test for that, and one that asks for `169.254.169.254`.

The AgentCore part is then a thin adapter:

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from journeyman.remote import RequestError, handle

app = BedrockAgentCoreApp()

@app.entrypoint
def invoke(payload, context=None):
    try:
        return handle(payload or {})
    except RequestError as exc:
        return {"error": str(exc)}      # a bad request is an answer, not a crashed session
```

## Things that went wrong, in order

**1. My first local test hit a different program.** I started the app locally and posted a review
request to port 8080. The reply was `{"error": "csv_path is required"}`. My app had failed to bind
("address already in use") and another local service on 8080 answered instead. Running the app on
another port fixed it. Check what is listening before you trust a local response.

**2. "Access denied. This Model is marked by provider as Legacy."** The Claude model I tried first in
the Bedrock playground had been moved to legacy status for accounts that had not used it recently.
Rather than guess another id, I listed what was actually active:

```bash
aws bedrock list-inference-profiles --region us-east-1 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'anthropic')].[inferenceProfileId,status]"
```

and then sent one tiny `converse` call to each candidate. One newer model listed as ACTIVE still
returned "not available for this account". `global.anthropic.claude-sonnet-4-6` answered in 1.5
seconds. Listed is not the same as usable; a one-token call settles it.

**3. `aws login` credentials need an extra package.** I used `aws login` instead of creating access
keys, which is the better choice for a short project. The CLI worked, but boto3 in my project's
environment raised `MissingDependencyException` until `botocore[crt]` was installed.

**4. The agent was calling the wrong region, and its report said so.** After a shift on Bedrock, the
report read "running in us-west-2". My CLI was configured for us-east-1. Journeyman read the region
only from `AWS_REGION` and fell back to us-west-2, and CloudWatch's Bedrock metrics confirmed the
tokens had gone there. It now uses the region the AWS configuration names. Printing the region in the
report is what caught it.

**5. My own reviewer flagged my deployment code.** The small client script that calls AgentCore had
no adaptive retry configured, and Journeyman's review raised it (AIE010: Bedrock-family clients get
throttled as a matter of course). Fixing it made me look at the client again, and boto3's default
60-second read timeout would have cut off an explain call. The first version of the region fix used a
bare `except Exception`, and the reviewer raised that too (AIE006). Using the tool on itself kept
paying off.

## Deploying

The starter toolkit's direct code deploy needs no Docker:

```bash
agentcore configure -e agentcore_app.py -n journeyman_reviewer -r us-east-1 \
  -rf requirements.txt --deployment-type direct_code_deploy --runtime PYTHON_3_13 --disable-memory
agentcore deploy
```

I deployed from a clean build directory holding only the entrypoint, the package and the requirements
file, so no virtual environment, benchmark data or local records went into the upload. It packaged
28 MB, created the S3 bucket and execution role, and reported success. One warning: the X-Ray trace
destination was still pending, so trace delivery could not be enabled on the first deploy. The
runtime itself worked immediately.

Calling it from a laptop is one boto3 call:

```python
client.invoke_agent_runtime(
    agentRuntimeArn=arn, qualifier="DEFAULT",
    runtimeSessionId=f"journeyman-{uuid.uuid4()}",   # needs at least 33 characters
    contentType="application/json", accept="application/json",
    payload=json.dumps({"archive": pack(repo), "name": repo.name, "mode": "explain"}).encode())
```

## What it did

On a small support-ticket app, review mode returned six findings in 4.7 seconds. Explain mode took
about 20 seconds: Claude read the flagged lines and wrote concrete fixes, starting with the one that
pages people first, `json.loads` on output that models routinely wrap in a code fence.

It also got one thing wrong, which is exactly why the findings and the explanation are kept apart. It
wrote that the OpenAI client has "no timeout at all". The Python SDK's default is 600 seconds. The
finding itself (no timeout set in this file) was right; a request that can hang for ten minutes is
still a problem. The prose around it was a model's, and it is labelled that way.

## What it cost

All of the Bedrock use for this, including an overnight-style fix run on Claude and its independent
checkers, came to 15 model invocations and about 28,000 input and 4,000 output tokens by CloudWatch's
Bedrock metrics: well under a dollar. AgentCore bills by use, so an idle runtime waiting for judges
costs close to nothing, and `agentcore destroy` removes it afterwards. Set a budget alert before you
start anyway.

## Takeaways

1. Decide what the model may decide before you deploy it. For a reviewer: facts from code, words from
   the model.
2. List, then call. A model listed as active can still be unavailable to your account.
3. Print where your agent is actually running. The wrong region was found by reading a report.
4. Point your own tools at your deployment code. Mine found a missing retry configuration and a
   swallowed exception in code I had just written.

Journeyman is MIT licensed and built on Strands Agents: https://github.com/Naseeruddeen634/journeyman
