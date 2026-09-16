"""Talking to Dynamics 365 / Dataverse: read cases, write the suggestion back.

Written against the Web API (OData v4) rather than a SDK, because the four things that decide
whether this integration survives production are protocol-level and easy to get wrong:

  - **Throttling.** Dataverse answers 429 with `Retry-After`. Honour the header; do not invent a
    backoff, and do not retry forever.
  - **Paging.** A query returns `@odata.nextLink` and nothing tells you it is there except reading
    for it. Code that reads `value` once silently processes the first page and calls it a day.
  - **Concurrency.** An update without `If-Match` overwrites whatever an agent changed while the
    model was thinking. The ETag from the read goes back on the write.
  - **Delta.** `$deltatoken` lets the poller ask for "what changed since last time" instead of
    scanning. The webhook is the primary path; this is the reconciliation loop behind it, because
    webhooks are lost occasionally and nobody notices until a customer does.

Nothing here holds a secret: a token provider is passed in, and the transport is injected so the
tests run without a tenant. Case text is never logged, only case ids.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator

MAX_RETRIES = 4
DEFAULT_BACKOFF = 2.0


class DataverseError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Dataverse {status}: {message}")
        self.status = status


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict:
        return json.loads(self.body or b"{}")


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as raw:
            return Response(raw.status, {k.lower(): v for k, v in raw.headers.items()}, raw.read())
    except urllib.error.HTTPError as exc:      # 429 and 4xx carry a body worth reading
        return Response(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read())


class Dataverse:
    """A small client. `token` is called per request so a refreshed token is picked up."""

    def __init__(self, base_url: str, token: Callable[[], str], transport=urllib_transport,
                 sleep=time.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.transport = transport
        self.sleep = sleep

    # ------------------------------------------------------------------ plumbing

    def _request(self, method: str, url: str, body: dict | None = None,
                 extra_headers: dict[str, str] | None = None) -> Response:
        headers = {
            "Authorization": f"Bearer {self.token()}",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            **(extra_headers or {}),
        }
        payload = json.dumps(body).encode() if body is not None else None
        for attempt in range(MAX_RETRIES + 1):
            response = self.transport(method, url, headers, payload)
            if response.status != 429 and response.status < 500:
                break
            if attempt == MAX_RETRIES:
                break
            # Dataverse tells you how long to wait. Guessing instead is how a client turns a
            # throttle into an outage for everyone sharing the service protection limit.
            wait = float(response.headers.get("retry-after", DEFAULT_BACKOFF * (2 ** attempt)))
            self.sleep(wait)
        if response.status >= 400:
            raise DataverseError(response.status, _error_message(response))
        return response

    # ------------------------------------------------------------------ reads

    def cases_changed_since(self, delta_token: str | None = None,
                            select: str = "incidentid,title,description,ticketnumber,versionnumber,"
                                          "modifiedon") -> tuple[list[dict], str | None]:
        """Cases changed since the last call, following every page.

        Returns (cases, next delta token). Store the token; the next call becomes "what changed",
        not "everything". The webhook stays the fast path; this catches what it dropped.
        """
        url = f"{self.base_url}/api/data/v9.2/incidents?$select={select}"
        headers = {"Prefer": "odata.track-changes"}
        if delta_token:
            url = f"{url}&$deltatoken={delta_token}"
        cases: list[dict] = []
        delta: str | None = None
        while True:
            response = self._request("GET", url, extra_headers=headers)
            data = response.json()
            cases.extend(data.get("value", []))
            if link := data.get("@odata.nextLink"):
                url = link                       # the server decides the page size, not us
                continue
            delta = _delta_token(data.get("@odata.deltaLink"))
            return cases, delta

    def case(self, case_id: str) -> tuple[dict, str | None]:
        """One case and its ETag, which is what makes a later update safe."""
        response = self._request("GET", f"{self.base_url}/api/data/v9.2/incidents({case_id})")
        return response.json(), response.headers.get("etag")

    # ------------------------------------------------------------------ writes

    def post_suggestion(self, case_id: str, title: str, text: str, external_id: str) -> str:
        """Attach the suggestion to the case as a note.

        `external_id` makes the write idempotent: a redelivered message updates the same note
        instead of adding a second one. Dataverse does that with an alternate key upsert.
        """
        url = (f"{self.base_url}/api/data/v9.2/annotations(journeyman_externalid='{external_id}')")
        body = {
            "subject": title,
            "notetext": text,
            "objectid_incident@odata.bind": f"/incidents({case_id})",
            "journeyman_externalid": external_id,
        }
        self._request("PATCH", url, body)
        return external_id

    def set_case_fields(self, case_id: str, fields: dict, etag: str | None) -> None:
        """Update a case, refusing to overwrite a change made since we read it."""
        headers = {"If-Match": etag} if etag else {"If-None-Match": "*"}
        try:
            self._request("PATCH", f"{self.base_url}/api/data/v9.2/incidents({case_id})", fields,
                          extra_headers=headers)
        except DataverseError as exc:
            if exc.status == 412:
                # Somebody edited the case while the model was thinking. Their edit wins; the
                # suggestion is still on the note, so nothing is lost.
                raise Conflict(case_id) from exc
            raise


class Conflict(RuntimeError):
    def __init__(self, case_id: str) -> None:
        super().__init__(f"case {case_id} changed since it was read; not overwriting")
        self.case_id = case_id


def to_event(case: dict, tenant_id: str, product_field: str = "journeyman_product") -> dict:
    """Dataverse's shape to ours. Keeping this in one function is what stops their field names
    leaking through the whole service."""
    return {
        "tenant_id": tenant_id,
        "case_id": case["incidentid"],
        "revision": int(case.get("versionnumber") or 0),
        "product": case.get(product_field) or "unknown",
        "title": case.get("title") or "",
        "body": case.get("description") or "",
    }


def iter_events(client: Dataverse, tenant_id: str, delta_token: str | None = None) -> Iterator[dict]:
    cases, _ = client.cases_changed_since(delta_token)
    for case in cases:
        yield to_event(case, tenant_id)


def _error_message(response: Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message", ""))[:300]
    except json.JSONDecodeError:
        return response.body[:200].decode("utf8", "replace")


def _delta_token(delta_link: str | None) -> str | None:
    if not delta_link or "$deltatoken=" not in delta_link:
        return None
    return delta_link.split("$deltatoken=", 1)[1].split("&", 1)[0]
