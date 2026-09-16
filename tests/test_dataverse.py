"""The Dataverse client, against a fake transport: no tenant, no credentials, no network.

These are the four failures that a Dataverse integration hits in its first month.
"""

import json

import pytest

from journeyman.support.dataverse import (Conflict, Dataverse, DataverseError, Response, iter_events,
                                          to_event)


class Fake:
    """Records requests and replays canned responses in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.slept = []

    def __call__(self, method, url, headers, body):
        self.requests.append({"method": method, "url": url, "headers": headers,
                              "body": json.loads(body) if body else None})
        status, payload, extra = self.replies.pop(0)
        return Response(status, extra, json.dumps(payload).encode() if payload is not None else b"")

    def client(self):
        return Dataverse("https://acme.crm4.dynamics.com", lambda: "token", self, self.slept.append)


def page(cases, next_link=None, delta=None):
    body = {"value": cases}
    if next_link:
        body["@odata.nextLink"] = next_link
    if delta:
        body["@odata.deltaLink"] = f"https://acme.crm4.dynamics.com/x?$deltatoken={delta}"
    return (200, body, {})


def test_every_page_is_read_not_just_the_first():
    fake = Fake([
        page([{"incidentid": "1"}], next_link="https://acme.crm4.dynamics.com/page2"),
        page([{"incidentid": "2"}], next_link="https://acme.crm4.dynamics.com/page3"),
        page([{"incidentid": "3"}], delta="D2026"),
    ])
    cases, delta = fake.client().cases_changed_since()
    assert [c["incidentid"] for c in cases] == ["1", "2", "3"]
    assert delta == "D2026", "the delta token is what makes the next poll cheap"
    assert fake.requests[1]["url"] == "https://acme.crm4.dynamics.com/page2"


def test_a_throttle_is_retried_after_the_period_the_server_asked_for():
    fake = Fake([
        (429, {"error": {"message": "service protection limit"}}, {"retry-after": "7"}),
        (429, {"error": {"message": "service protection limit"}}, {"retry-after": "3"}),
        page([{"incidentid": "1"}], delta="D1"),
    ])
    cases, _ = fake.client().cases_changed_since()
    assert [c["incidentid"] for c in cases] == ["1"]
    assert fake.slept == [7.0, 3.0], "honour Retry-After rather than inventing a backoff"


def test_it_gives_up_instead_of_retrying_a_throttle_forever():
    fake = Fake([(429, {"error": {"message": "still limited"}}, {"retry-after": "1"})] * 5)
    with pytest.raises(DataverseError) as exc:
        fake.client().cases_changed_since()
    assert exc.value.status == 429
    assert len(fake.slept) == 4, "four waits, then the caller is told"


def test_a_case_edited_while_we_were_thinking_is_not_overwritten():
    fake = Fake([(412, {"error": {"message": "precondition failed"}}, {})])
    with pytest.raises(Conflict, match="not overwriting"):
        fake.client().set_case_fields("case-1", {"title": "x"}, etag='W/"1234"')
    assert fake.requests[0]["headers"]["If-Match"] == 'W/"1234"'


def test_the_suggestion_note_is_upserted_so_a_redelivery_does_not_duplicate_it():
    fake = Fake([(204, None, {})])
    fake.client().post_suggestion("case-1", "Journeyman triage", "queue: identity", "sug-7")
    request = fake.requests[0]
    assert request["method"] == "PATCH"
    assert "journeyman_externalid='sug-7'" in request["url"]
    assert request["body"]["objectid_incident@odata.bind"] == "/incidents(case-1)"


def test_their_field_names_stop_at_the_boundary():
    case = {"incidentid": "abc", "versionnumber": 42, "title": "Cannot sign in",
            "description": "AADSTS50011", "journeyman_product": "portal"}
    assert to_event(case, "acme") == {"tenant_id": "acme", "case_id": "abc", "revision": 42,
                                      "product": "portal", "title": "Cannot sign in",
                                      "body": "AADSTS50011"}


def test_a_case_with_no_product_or_body_still_becomes_a_valid_event():
    event = to_event({"incidentid": "abc"}, "acme")
    assert event["product"] == "unknown" and event["body"] == "" and event["revision"] == 0


def test_iter_events_hands_the_service_what_ingest_expects():
    fake = Fake([page([{"incidentid": "1", "versionnumber": 3, "title": "t", "description": "b"}],
                      delta="D1")])
    events = list(iter_events(fake.client(), "acme"))
    assert events[0]["case_id"] == "1" and events[0]["revision"] == 3


def test_every_request_carries_a_fresh_token_and_the_odata_headers():
    tokens = iter(["t1", "t2"])
    fake = Fake([page([], delta="D1"), page([], delta="D2")])
    client = Dataverse("https://acme.crm4.dynamics.com", lambda: next(tokens), fake, fake.slept.append)
    client.cases_changed_since()
    client.cases_changed_since()
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer t1"
    assert fake.requests[1]["headers"]["Authorization"] == "Bearer t2", "a refreshed token is picked up"
    assert fake.requests[0]["headers"]["OData-Version"] == "4.0"
