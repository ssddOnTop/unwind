"""Frozen-contract acceptance, edge cases and extension/concurrency conformance."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
import pytest

from companion.bootstrap import UnavailableSynthesis
from sample_data import build_service
from companion.contracts import SCHEMA, validate
from sample_data import BATCH, EXAMPLE
from companion.core import Registry
from companion.domain import iso, synthesis
from sample_data import create_app
from companion.scheduling import NotificationScheduler

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, replacements={"synthesis": UnavailableSynthesis()}, clock=lambda: NOW)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def state(c):
    r = c.get("/state")
    assert r.status_code == 200, r.text
    validate("state", r.json())
    return r.json()


def request(c, path, method="post", rid=None, epoch=None, **fields):
    body = {"schema_version": "1", "request_id": rid or str(uuid4()), "data_epoch": epoch or state(c)["data_epoch"], **fields}
    return c.request(method, path, json=body)


def seed(c):
    r = request(c, "/demo", action="seed", fixture_set="representative-v1", consent_demo=True)
    assert r.status_code == 200, r.text
    return r.json()


def prefs(c, **changes):
    p = state(c)["preferences"]
    p.update(changes)
    r = request(c, "/preferences", "put", preferences=p)
    assert r.status_code == 200, r.text
    return r.json()


def batch(c, observations, **extra):
    return request(c, "/observations", observations=observations, source_updates=extra.pop("source_updates", []), **extra)


def feedback(c, **changes):
    fields = dict(card_id="purchase_recurrence", kind="correctness", value="partial", scope="event",
                  event_ref={"observation_id": "demo-purchase-1", "revision": 1}, attribution="shared", note="")
    fields.update(changes)
    return request(c, "/feedback", **fields)


def revision(o, **changes):
    n = deepcopy(o)
    n.update(revision=o["revision"] + 1, supersedes={"observation_id": o["id"], "revision": o["revision"]})
    n.update(changes)
    return n


def test_fixture_and_example_schema():
    validate("state", EXAMPLE)
    validate("observation_batch", BATCH)


def test_empty_state_read_only_and_health(client):
    s = state(client)
    assert state(client) == s
    assert s["revision"] == 0 and not s["observations"]
    assert s["sources"][0]["consent"] == "not_requested"
    assert s["ledger"][1]["recorded_steps"] is None
    assert s["ledger"][1]["days_with_records"] == 0
    assert s["process"]["status"] == "idle"
    h = client.get("/health").json()
    validate("health", h)
    assert h["status"] == "ok" and len(h["capabilities"]) == 8
    assert {x["id"] for x in h["capabilities"]} == {"persistence", "scenarios", "feedback", "demo", "healthkit", "plaid", "email", "synthesis"}


def test_demo_matches_fixture_semantics(client):
    s = seed(client)
    for name in ("preferences", "sources", "observations", "ledger", "limitations", "notification_intents", "evaluation_date"):
        assert s[name] == EXAMPLE[name], name
    assert s["ledger"][0]["purchase_count"] == 3
    assert s["ledger"][1]["recorded_steps"] == 13800
    assert s["ledger"][1]["suggestion"]["status"] == "suppressed"
    validate("state", s)


def test_exact_routes_and_generated_openapi(client):
    spec = client.get("/openapi.json").json()
    expected = {"/health": "get", "/state": "get", "/observations": "post", "/feedback": "post", "/preferences": "put", "/process": "post", "/demo": "post", "/data": "delete",
                "/onboarding/v1": "get", "/onboarding/v1/actions": "post"}
    assert set(spec["paths"]) == set(expected)
    for path, method in expected.items():
        assert set(spec["paths"][path]) == {method}
        op = spec["paths"][path][method]
        assert "security" not in op
        for status in ("200", "400", "404", "409", "413", "422", "500"):
            assert "application/json" in op["responses"][status]["content"]
    assert client.get("/docs").status_code == 200
    for path in ("/redoc", "/docs/oauth2-redirect", "/jobs", "/unknown"):
        r = client.get(path)
        assert r.status_code == 404
        validate("error", r.json())
    schema = spec["paths"]["/state"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    Draft202012Validator({**schema, "components": spec["components"]}, format_checker=FormatChecker()).validate(state(client))
    assert not client.get("/state", headers={"Origin": "https://example.com"}).headers.get("access-control-allow-origin")
    assert client.get("/state", headers={"Host": "example.com"}).status_code == 400


@pytest.mark.parametrize("raw", [b"{", b"", b"null", b"[]", b'{"a":NaN}', b'{"a":1,"a":2}', b'\xff'])
def test_malformed_json(client, raw):
    r = client.post("/observations", content=raw, headers={"content-type": "application/json"})
    assert r.status_code in (400, 422)
    validate("error", r.json())


def test_body_limits_query_and_framework_errors(client):
    r = client.post("/observations", content=b"x" * (1024 * 1024 + 1))
    assert r.status_code == 413
    validate("error", r.json())
    assert client.get("/state?x=1").status_code == 422
    r = client.post("/state", json={})
    assert r.status_code == 422
    r = client.put("/state")
    assert r.status_code == 405
    validate("error", r.json())


@pytest.mark.parametrize("change", [
    {"schema_version": "2"}, {"raw_payload": "forbidden"}, {"observations": None},
    {"request_id": "bad id"}, {"source_updates": [{"unexpected": True}]},
])
def test_structural_request_rejection(client, change):
    b = {"schema_version": "1", "request_id": "test", "data_epoch": state(client)["data_epoch"], "observations": [], "source_updates": []}
    b.update(change)
    r = client.post("/observations", json=b)
    assert r.status_code == 422
    validate("error", r.json())
    assert r.json()["error"]["code"] == ("unsupported_version" if "schema_version" in change else "invalid_request")


@pytest.mark.parametrize("mutate", [
    lambda o: o["activity"].update(unit="minutes"),
    lambda o: o.update(occurred_at="2026-11-08T00:00:00Z"),
    lambda o: o.update(timezone="Mars/Olympus"),
    lambda o: o.update(local_date="2026-02-30"),
    lambda o: o.update(kind="purchase"),
    lambda o: o["activity"].update(value=-1),
    lambda o: o["activity"].update(value=True),
    lambda o: o["activity"].update(value="4200"),
    lambda o: o.update(raw_samples=[]),
    lambda o: o.update(time_precision="datetime", occurred_at="2026-11-09T12:00:00Z"),
    lambda o: o["provenance"].update(method="provider_record"),
])
def test_invalid_observation_atomic(client, mutate):
    prefs(client)
    before = state(client)
    good = deepcopy(BATCH["observations"][0])
    invalid = deepcopy(BATCH["observations"][3])
    mutate(invalid)
    r = batch(client, [good, invalid])
    assert r.status_code == 422, r.text
    assert state(client) == before


def test_consent_required_and_accept_batch_duplicates(client):
    assert batch(client, BATCH["observations"]).status_code == 409
    prefs(client)
    r = batch(client, BATCH["observations"], rid="first")
    assert r.status_code == 200
    assert (r.json()["accepted"], r.json()["ignored"]) == (6, 0)
    original = r.json()
    retry = batch(client, BATCH["observations"], rid="first")
    assert retry.json() == original
    second = batch(client, BATCH["observations"])
    assert (second.json()["accepted"], second.json()["ignored"]) == (0, 6)
    assert second.json()["state"] == original["state"]
    assert batch(client, [], rid="first").json()["error"]["code"] == "idempotency_conflict"


def test_seed_replay_noop_and_preferences_preserved(client):
    seed(client)
    prefs(client, notes="user choice")
    before = state(client)
    assert seed(client) == before
    r = batch(client, BATCH["observations"], source_updates=BATCH["source_updates"])
    assert r.status_code == 200
    assert r.json()["ignored"] == 6 and r.json()["state"] == before


def test_duplicate_conflict_rolls_back_entire_batch(client):
    prefs(client)
    before = state(client)
    first = deepcopy(BATCH["observations"][0])
    changed = deepcopy(first)
    changed["purchase"]["merchant"] = "Different synthetic merchant"
    r = batch(client, [first, changed])
    assert r.status_code == 409 and r.json()["error"]["code"] == "revision_conflict"
    assert state(client) == before
    r = batch(client, [first, first])
    assert (r.json()["accepted"], r.json()["ignored"]) == (1, 1)


def test_revision_tombstone_and_immutable_original(client):
    s = seed(client)
    original = deepcopy(s["observations"][0])
    updated = revision(original, household_attribution="shared")
    r = batch(client, [updated])
    assert r.status_code == 200, r.text
    assert len(r.json()["state"]["observations"]) == 7
    assert original in r.json()["state"]["observations"]
    assert r.json()["state"]["ledger"][0]["purchase_count"] == 3
    assert r.json()["state"]["ledger"][0]["suggestion"] is None
    removed = revision(updated, operation="remove", lifecycle="removed", purchase=None, activity=None)
    r = batch(client, [removed])
    assert r.status_code == 200, r.text
    assert r.json()["state"]["ledger"][0]["purchase_count"] == 2
    assert len(r.json()["state"]["observations"]) == 8
    assert batch(client, [revision(removed, revision=5)]).status_code == 409


def test_pending_posted_reverse_batch_order_and_single_successor(client):
    prefs(client)
    pending = deepcopy(BATCH["observations"][0])
    pending.update(id="pending", lifecycle="pending")
    pending["purchase"]["posted_date"] = None
    posted = deepcopy(BATCH["observations"][0])
    posted.update(id="posted", supersedes={"observation_id": "pending", "revision": 1})
    r = batch(client, [posted, pending])
    assert r.status_code == 200, r.text
    assert r.json()["state"]["ledger"][0]["purchase_count"] == 1
    fork = deepcopy(posted)
    fork["id"] = "fork"
    before = state(client)
    assert batch(client, [fork]).status_code == 409
    assert state(client) == before


def test_unknown_supersedes_and_wrong_identity(client):
    seed(client)
    o = revision(BATCH["observations"][0], supersedes={"observation_id": "absent", "revision": 1})
    assert batch(client, [o]).status_code == 409
    o = revision(BATCH["observations"][0], id="new-posted", revision=1)
    assert batch(client, [o]).status_code == 409  # cannot supersede a posted record under a new ID


def test_one_authoritative_daily_aggregate(client):
    prefs(client)
    o = deepcopy(BATCH["observations"][3])
    second = deepcopy(o)
    second["id"] = "another-phone"
    assert batch(client, [o, second]).status_code == 409
    assert not state(client)["observations"]


def test_feedback_overlay_immutable_correctness_and_usefulness(client):
    s = seed(client)
    r = feedback(client)
    assert r.status_code == 200
    s2 = r.json()
    assert s2["observations"] == s["observations"]
    assert s2["ledger"][0]["purchase_count"] == 3
    assert s2["ledger"][0]["suggestion"] is None
    assert s2["ledger"][0]["feedback_ids"]
    r = feedback(client, value="correct", attribution="self")
    assert r.json()["ledger"][0]["suggestion"]["status"] == "eligible"
    r = feedback(client, scope="card", event_ref=None, attribution=None, kind="usefulness", value="not_useful")
    assert r.json()["ledger"][0]["suggestion"]["status"] == "unavailable"
    r = feedback(client, scope="card", event_ref=None, attribution=None, kind="usefulness", value="useful")
    assert r.json()["ledger"][0]["suggestion"]["status"] == "eligible"
    assert len(r.json()["feedback"]) == 4


@pytest.mark.parametrize("changes", [
    {"kind": "usefulness", "value": "correct"}, {"scope": "scenario"},
    {"event_ref": None}, {"kind": "suppress", "value": "stop", "scope": "event"},
    {"scope": "card", "event_ref": None, "attribution": "self"},
])
def test_invalid_feedback_combinations(client, changes):
    seed(client)
    before = state(client)
    assert feedback(client, **changes).status_code == 422
    assert state(client) == before


def test_feedback_missing_card_and_event(client):
    seed(client)
    assert feedback(client, card_id="absent").status_code == 404
    assert feedback(client, event_ref={"observation_id": "absent", "revision": 1}).status_code == 404
    assert feedback(client, card_id="recorded_steps").status_code == 404


def test_suppress_unsuppress_and_constraints(client):
    seed(client)
    r = feedback(client, kind="suppress", value="stop", scope="scenario", event_ref=None, attribution=None)
    assert r.json()["preferences"]["suppressed_scenario_ids"] == ["purchase_recurrence"]
    assert r.json()["ledger"][0]["suggestion"]["status"] == "suppressed"
    s = prefs(client, suppressed_scenario_ids=[])
    assert s["ledger"][0]["suggestion"]["status"] == "eligible"
    s = prefs(client, food_suggestions=False, movement_restriction=False)
    assert s["ledger"][0]["suggestion"]["status"] == "suppressed"
    assert s["ledger"][1]["suggestion"]["status"] == "eligible"
    assert s["observations"] == EXAMPLE["observations"]


def test_source_revocation_and_reenable(client):
    seed(client)
    s = prefs(client, enabled_source_ids=[])
    assert s["sources"][0]["consent"] == "revoked"
    assert s["observations"] == EXAMPLE["observations"]
    assert s["ledger"][0]["purchase_count"] == 0
    assert s["ledger"][1]["recorded_steps"] is None
    assert batch(client, BATCH["observations"]).json()["error"]["code"] == "consent_required"
    s = prefs(client, enabled_source_ids=["demo"])
    assert s["ledger"][0]["purchase_count"] == 3


@pytest.mark.parametrize("source", ["plaid", "email"])
def test_unconfigured_source_rejected_atomically(client, source):
    before = state(client)
    p = deepcopy(before["preferences"])
    p["enabled_source_ids"].append(source)
    r = request(client, "/preferences", "put", preferences=p)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_configured"
    assert state(client) == before


def healthkit_record():
    o = deepcopy(BATCH["observations"][3])
    o.update(id="healthkit-day", source_id="healthkit", mode="live")
    o["provenance"].update(adapter="healthkit_daily_steps", method="daily_cumulative_sum")
    return o


def test_healthkit_selection_empty_unknown_and_reset_conflict(client):
    seed(client)
    s = prefs(client, enabled_source_ids=["demo", "healthkit"])
    assert s["sources"][1]["consent"] == "granted"
    assert s["ledger"][1]["mode"] == "live" and s["ledger"][1]["recorded_steps"] is None
    o = healthkit_record()
    r = batch(client, [o])
    assert r.status_code == 200, r.text
    assert r.json()["state"]["ledger"][1]["recorded_steps"] == 4200
    assert request(client, "/process", synthesis_mode="scripted").status_code == 409
    assert request(client, "/demo", action="reset", fixture_set="representative-v1", consent_demo=True).status_code == 409
    prefs(client, enabled_source_ids=["demo"])
    assert request(client, "/demo", action="reset", fixture_set="representative-v1", consent_demo=True).status_code == 409
    assert state(client)["ledger"][1]["recorded_steps"] == 13800


def test_source_updates_empty_errors_and_old_reports(client):
    prefs(client, enabled_source_ids=["healthkit"])
    update = deepcopy(BATCH["source_updates"][0])
    update.update(source_id="healthkit", read_access="unknown", authorization_requested=True, query_status="empty")
    r = batch(client, [], source_updates=[update])
    assert r.status_code == 200
    source = r.json()["state"]["sources"][1]
    assert source["status"] == "empty" and source["coverage"] == "unknown"
    success = source["last_success_at"]
    update.update(attempted_at="2026-11-14T13:00:00Z", query_status="error", error_code="protected_data")
    r = batch(client, [], source_updates=[update])
    source = r.json()["state"]["sources"][1]
    assert source["last_success_at"] == success and source["reason"] == "protected_data"
    before = state(client)
    update["attempted_at"] = "2026-11-14T11:00:00Z"
    assert batch(client, [], source_updates=[update]).json()["state"] == before
    update["read_access"] = "samples_readable"
    assert batch(client, [], source_updates=[update]).status_code == 422


def test_timezone_exclusion_and_invalid_timezone(client):
    seed(client)
    s = prefs(client, timezone="America/Los_Angeles")
    assert s["ledger"][0]["purchase_count"] == 0
    assert s["ledger"][1]["recorded_steps"] is None
    assert any("timezone" in x for x in s["ledger"][1]["limitations"])
    assert s["observations"] == EXAMPLE["observations"]
    p = s["preferences"]
    p["timezone"] = "NoSuch/Zone"
    assert request(client, "/preferences", "put", preferences=p).status_code == 422


def test_explicit_zero_is_recorded_not_missing(client):
    prefs(client)
    o = deepcopy(BATCH["observations"][3])
    o["activity"]["value"] = 0
    r = batch(client, [o])
    c = r.json()["state"]["ledger"][1]
    assert c["recorded_steps"] == 0 and c["days_with_records"] == 1
    assert c["status"] == "observed"


def test_recurrence_requires_fourteen_day_span(client):
    prefs(client)
    obs = deepcopy(BATCH["observations"][:3])
    for o, d in zip(obs, ("2026-11-01", "2026-11-05", "2026-11-14")):
        o["local_date"] = d
        o["purchase"].update(authorized_date=d, posted_date=d)
    s = batch(client, obs).json()["state"]
    assert s["ledger"][0]["inferred"] is None
    assert s["ledger"][0]["suggestion"] is None


def test_refund_and_pending_excluded(client):
    prefs(client)
    obs = deepcopy(BATCH["observations"][:3])
    obs[0]["purchase"]["is_refund"] = True
    obs[1]["lifecycle"] = "pending"
    obs[1]["purchase"]["posted_date"] = None
    assert batch(client, obs).json()["state"]["ledger"][0]["purchase_count"] == 1


def test_delete_rotation_clean_receipt_and_no_data_in_database(client):
    seed(client)
    old = state(client)["data_epoch"]
    deleted = request(client, "/data", "delete", rid="delete-once", epoch=old, confirm="delete_all")
    assert deleted.status_code == 200
    s = deleted.json()
    assert s["data_epoch"] != old and not s["observations"] and not s["feedback"]
    assert not s["notification_intents"] and s["process"]["status"] == "idle"
    assert s["preferences"]["notes"] == "" and s["sources"][0]["consent"] == "not_requested"
    assert request(client, "/data", "delete", rid="delete-once", epoch=old, confirm="delete_all").json() == s
    assert batch(client, [], epoch=old).json()["error"]["code"] == "epoch_mismatch"
    path = client.app.state.service.registry._extensions[("persistence", "1")].path
    assert b"Synthetic Cafe" not in path.read_bytes()
    db = sqlite3.connect(path)
    assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    db.close()


def test_reset_rotates_epoch_and_retry_safe(client):
    seed(client)
    old = state(client)["data_epoch"]
    args = dict(action="reset", fixture_set="representative-v1", consent_demo=True, epoch=old, rid="reset-once")
    s = request(client, "/demo", **args).json()
    assert s["data_epoch"] != old
    assert request(client, "/demo", **args).json() == s
    args["rid"] = "another-reset"
    assert request(client, "/demo", **args).status_code == 409


def test_demo_modified_observations_conflict(client):
    seed(client)
    assert batch(client, [revision(BATCH["observations"][0], household_attribution="shared")]).status_code == 200
    assert request(client, "/demo", action="seed", fixture_set="representative-v1", consent_demo=True).status_code == 409


def test_notifications_cancelled_never_reissued_same_day(client):
    seed(client)
    s = prefs(client, notifications={"enabled": True, "local_time": "18:00"})
    assert len(s["notification_intents"]) == 1
    assert s["notification_intents"][0]["scenario_id"] == "purchase_recurrence"
    s = prefs(client, food_suggestions=False)
    assert not s["notification_intents"]
    s = prefs(client, food_suggestions=True)
    assert not s["notification_intents"]
    assert not request(client, "/process", synthesis_mode="disabled").json()["notification_intents"]


def test_notification_time_passed_skips_today(client):
    seed(client)
    s = prefs(client, notifications={"enabled": True, "local_time": "06:00"})
    assert not s["notification_intents"]


@pytest.mark.parametrize("day,time_value,expected", [("2026-03-08", "02:30", 0), ("2026-11-01", "01:30", 1)])
async def test_notification_dst(day, time_value, expected):
    s = deepcopy(EXAMPLE)
    s["evaluation_date"] = day
    s["preferences"]["notifications"] = {"enabled": True, "local_time": time_value}
    result = await NotificationScheduler().handle("plan", {"state": s, "issued": [], "now": day + "T04:00:00Z"})
    assert len(result["intents"]) == expected
    if expected:
        assert result["intents"][0]["scheduled_at"].endswith("-04:00")


async def test_notification_seven_local_day_cooldown():
    s = deepcopy(EXAMPLE)
    s["preferences"]["notifications"]["enabled"] = True
    issued = [{"id": "prior", "suggestion_id": "purchase_recurrence.plan", "date": "2026-11-08"}]
    scheduler = NotificationScheduler()
    r = await scheduler.handle("plan", {"state": s, "issued": issued, "now": "2026-11-14T12:00:00Z"})
    assert not r["intents"]
    s["evaluation_date"] = "2026-11-15"
    r = await scheduler.handle("plan", {"state": s, "issued": issued, "now": "2026-11-15T12:00:00Z"})
    assert len(r["intents"]) == 1


def test_restart_retains_state_and_receipt(tmp_path):
    def app():
        return create_app(tmp_path, replacements={"synthesis": UnavailableSynthesis()}, clock=lambda: NOW)
    with TestClient(app(), base_url="http://127.0.0.1") as c:
        s = seed(c)
        r = batch(c, BATCH["observations"], rid="replay")
        before = state(c)
    with TestClient(app(), base_url="http://127.0.0.1") as c:
        assert state(c) == before
        assert batch(c, BATCH["observations"], rid="replay").json() == r.json()


def test_restart_recovers_persistent_running_job(tmp_path):
    app = create_app(tmp_path, replacements={"synthesis": UnavailableSynthesis()}, clock=lambda: NOW)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        seed(c)
    db = sqlite3.connect(tmp_path / "state.sqlite")
    doc = json.loads(db.execute("SELECT body FROM snapshot").fetchone()[0])
    doc["state"]["process"].update(status="running", completed_at=None)
    doc["state"]["process"]["synthesis"].update(status="running", reason=None)
    db.execute("UPDATE snapshot SET body=?", (json.dumps(doc),))
    db.commit()
    db.close()
    with TestClient(create_app(tmp_path, replacements={"synthesis": UnavailableSynthesis()}, clock=lambda: NOW), base_url="http://127.0.0.1") as c:
        s = state(c)
        assert s["process"]["status"] == "failed"
        assert s["process"]["synthesis"]["reason"] == "internal_error"
        assert s["ledger"][1]["recorded_steps"] == 13800


def test_retention_on_restart_and_incoming_rejection(tmp_path):
    clock = [NOW]
    def app():
        return create_app(tmp_path, replacements={"synthesis": UnavailableSynthesis()}, clock=lambda: clock[0])
    with TestClient(app(), base_url="http://127.0.0.1") as c:
        prefs(c)
        assert batch(c, BATCH["observations"]).status_code == 200
        feedback(c)
    clock[0] = NOW + timedelta(days=100)
    with TestClient(app(), base_url="http://127.0.0.1") as c:
        s = state(c)
        assert not s["observations"] and not s["feedback"]
        assert batch(c, BATCH["observations"]).status_code == 422


class ControlledSynthesis:
    id, version, status = "synthesis", "1", "available"
    def __init__(self, outcome="success"):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.outcome = outcome
        self.clears = 0
        self.calls = 0

    async def handle(self, operation, payload):
        if operation == "clear":
            self.clears += 1
            return {"cleared": True}
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.outcome == "raise":
            raise RuntimeError("private provider error must not escape")
        if self.outcome == "invalid":
            return {"unexpected": "bad"}
        result = synthesis(payload["mode"], "completed")
        if self.outcome == "dangling":
            result["drafts"] = [{"id": "draft", "text": "Test", "card_ids": ["nonexistent"], "evidence": []}]
        return result


async def run_with_service(tmp_path, extension, timeout=30):
    service = build_service(tmp_path, replacements={"synthesis": extension}, clock=lambda: NOW, synthesis_timeout=timeout)
    await service.start()
    s = await service.state()
    b = {"schema_version": "1", "request_id": "process", "data_epoch": s["data_epoch"], "synthesis_mode": "scripted"}
    return service, b


async def test_processing_lock_released_busy_and_stale_context(tmp_path):
    ext = ControlledSynthesis()
    service, body = await run_with_service(tmp_path, ext)
    task = asyncio.create_task(service.mutate("/process", body))
    await ext.entered.wait()
    assert (await service.state())["process"]["status"] == "running"
    from companion.contracts import DomainError
    with pytest.raises(DomainError) as err:
        await service.mutate("/process", {**body, "request_id": "another"})
    assert err.value.code == "busy"
    s = await service.state()
    p = deepcopy(s["preferences"])
    p["food_suggestions"] = False
    await service.mutate("/preferences", {"schema_version": "1", "request_id": "prefs", "data_epoch": s["data_epoch"], "preferences": p})
    ext.release.set()
    final = await task
    assert final["process"]["synthesis"]["reason"] == "stale_context"
    assert not final["preferences"]["food_suggestions"]
    assert ext.calls == 1
    assert await service.mutate("/process", body) == final
    await service.close()


async def test_delete_cancels_process_and_prevents_epoch_publication(tmp_path):
    ext = ControlledSynthesis()
    service, body = await run_with_service(tmp_path, ext)
    task = asyncio.create_task(service.mutate("/process", body))
    await ext.entered.wait()
    deleted = await service.mutate("/data", {"schema_version": "1", "request_id": "delete", "data_epoch": body["data_epoch"], "confirm": "delete_all"})
    final = await task
    assert final == deleted
    assert final["data_epoch"] != body["data_epoch"] and final["process"]["status"] == "idle"
    assert ext.clears >= 1
    await service.close()


@pytest.mark.parametrize("outcome,reason", [("raise", "internal_error"), ("invalid", "invalid_output"), ("dangling", "invalid_output")])
async def test_provider_failure_keeps_ledger(tmp_path, outcome, reason):
    ext = ControlledSynthesis(outcome)
    ext.release.set()
    service, body = await run_with_service(tmp_path, ext)
    before = (await service.state())["ledger"]
    s = await service.mutate("/process", body)
    assert s["process"]["status"] == "completed"
    assert s["process"]["synthesis"]["reason"] == reason
    assert s["ledger"] == before
    await service.close()


async def test_synthesis_timeout_bounded(tmp_path):
    ext = ControlledSynthesis()
    service, body = await run_with_service(tmp_path, ext, timeout=.01)
    s = await service.mutate("/process", body)
    assert s["process"]["synthesis"]["reason"] == "timeout"
    await service.close()


async def test_registry_duplicate_version_and_public_replacement():
    reg = Registry()
    ext = UnavailableSynthesis()
    reg.register(ext)
    with pytest.raises(ValueError):
        reg.register(ext)
    assert (await reg.dispatch("synthesis", "synthesize", {"mode": "disabled"}))["reason"] == "disabled"
    with pytest.raises(KeyError):
        await reg.dispatch("synthesis", "synthesize", {}, version="2")


async def test_replaceable_scenarios_through_public_handle(tmp_path):
    class ScenarioDouble:
        id, version, status = "scenarios", "1", "available"
        async def handle(self, operation, payload):
            assert operation == "evaluate"
            s = deepcopy(payload["state"])
            s["ledger"] = deepcopy(EXAMPLE["ledger"])
            for c in s["ledger"]:
                c.update(evidence=[], feedback_ids=[], suggestion=None, observed="Replacement extension observation.")
            return {"state": s}
    service = build_service(tmp_path, replacements={"scenarios": ScenarioDouble(), "synthesis": UnavailableSynthesis()}, clock=lambda: NOW)
    await service.start()
    s = await service.state()
    validate("state", s)
    assert s["ledger"][0]["observed"] == "Replacement extension observation."
    await service.close()
