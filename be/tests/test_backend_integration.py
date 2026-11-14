"""Real SDK composition and additional public-boundary regression tests."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import socket
import sqlite3

from fastapi.testclient import TestClient
import httpx
import pytest

from companion.bootstrap import UnavailableSynthesis
from sample_data import build_service
from companion.contracts import DomainError, validate
from sample_data import BATCH, EXAMPLE
from companion.domain import iso, synthesis
from companion.extensions.scenarios import ScenariosExtension
from sample_data import create_app
from test_backend import NOW, ControlledSynthesis, batch, feedback, prefs, request, revision, seed, state


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, replacements={"synthesis": UnavailableSynthesis()}, clock=lambda: NOW), base_url="http://127.0.0.1") as c:
        yield c


def test_real_sdk_scripted_api_no_network_and_checkpoint_erasure(tmp_path, monkeypatch):
    # Construct the in-process transport before disabling real IPv4/IPv6 connects.
    with TestClient(create_app(tmp_path, clock=lambda: NOW), base_url="http://127.0.0.1") as c:
        seed(c)
        original = socket.socket.connect
        def deny_network(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                raise AssertionError("Scripted synthesis must not use the network")
            return original(sock, address)
        monkeypatch.setattr(socket.socket, "connect", deny_network)
        before = state(c)
        s = request(c, "/process", synthesis_mode="scripted").json()
        validate("state", s)
        assert s["process"]["synthesis"]["status"] == "completed", s["process"]
        assert s["process"]["synthesis"]["drafts"]
        assert "Scripted" in s["process"]["synthesis"]["drafts"][0]["text"]
        for field in ("observations", "ledger", "feedback", "preferences", "notification_intents"):
            assert s[field] == before[field]
        checkpoint = tmp_path / "checkpoints.sqlite"
        assert checkpoint.exists()
        # Actual SQLite saver persisted a completed graph, not a mocked success marker.
        db = sqlite3.connect(checkpoint)
        assert db.execute("SELECT count(*) FROM checkpoints").fetchone()[0] > 0
        db.close()
        s = prefs(c, food_suggestions=False)
        assert not s["process"]["synthesis"]["drafts"]
        assert not checkpoint.exists()
        request(c, "/data", "delete", confirm="delete_all")
        assert not list(tmp_path.glob("checkpoints.sqlite*"))


def test_scripted_accepts_empty_healthkit_card_with_only_fixture_evidence(tmp_path):
    with TestClient(create_app(tmp_path, clock=lambda: NOW), base_url="http://127.0.0.1") as c:
        seed(c)
        s = prefs(c, enabled_source_ids=["demo", "healthkit"])
        assert s["ledger"][1]["mode"] == "live"
        assert not s["ledger"][1]["evidence"]
        assert all(o["mode"] == "fixture" for o in s["observations"])
        r = request(c, "/process", synthesis_mode="scripted")
        assert r.status_code == 200
        s = r.json()
        assert s["process"]["synthesis"]["status"] == "completed", s["process"]
        assert s["process"]["synthesis"]["drafts"]
        assert all("recorded_steps" not in d["card_ids"] for d in s["process"]["synthesis"]["drafts"])


def test_live_missing_credentials_real_composition(tmp_path, monkeypatch):
    for name in ("OPENROUTER_API_KEY", "REQUESTY_API_KEY", "COMPANION_LLM_PROVIDER", "COMPANION_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(tmp_path, clock=lambda: NOW), base_url="http://127.0.0.1") as c:
        seed(c)
        s = request(c, "/process", synthesis_mode="live").json()
        assert s["process"]["status"] == "completed"
        assert s["process"]["synthesis"]["reason"] == "no_credentials"
        assert s["ledger"][1]["recorded_steps"] == 13800


@pytest.mark.parametrize("mode", ["disabled", "live"])
def test_no_key_modes_do_not_break_ledger(client, mode):
    seed(client)
    before = state(client)["ledger"]
    s = request(client, "/process", synthesis_mode=mode).json()
    assert s["process"]["status"] == "completed"
    assert s["process"]["synthesis"]["reason"] == ("disabled" if mode == "disabled" else "no_credentials")
    assert s["ledger"] == before


def test_canonical_json_retry_ignores_spacing_and_key_order(client):
    prefs(client)
    b = {"schema_version": "1", "request_id": "canonical", "data_epoch": state(client)["data_epoch"],
         "observations": BATCH["observations"], "source_updates": []}
    first = client.post("/observations", content=json.dumps(b), headers={"content-type": "application/json"})
    second = client.post("/observations", content=json.dumps(dict(reversed(list(b.items()))), indent=4), headers={"content-type": "application/json"})
    assert first.status_code == 200 and second.json() == first.json()


def test_card_revision_changes_only_for_semantic_change(client):
    s = seed(client)
    revisions = [c["revision"] for c in s["ledger"]]
    prefs(client, notes="Explicit context; not an extracted medical rule.")
    assert [c["revision"] for c in state(client)["ledger"]] == revisions
    prefs(client, food_suggestions=False)
    new = [c["revision"] for c in state(client)["ledger"]]
    assert new[0] == revisions[0] + 1
    assert new[1] == revisions[1]


def test_generated_openapi_preserves_observation_conditionals(client):
    from jsonschema import Draft202012Validator, FormatChecker
    spec = client.get("/openapi.json").json()
    schema = {"$ref": "#/components/schemas/observation", "components": spec["components"]}
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    good = deepcopy(BATCH["observations"][0])
    assert validator.is_valid(good)
    bad = deepcopy(good)
    bad["occurred_at"] = "2026-10-22T12:00:00Z"
    assert not validator.is_valid(bad)
    bad = deepcopy(good)
    bad["kind"] = "activity"
    assert not validator.is_valid(bad)


def test_date_precision_purchase_semantics(client):
    prefs(client)
    o = deepcopy(BATCH["observations"][0])
    o["purchase"]["authorized_date"] = "2026-10-21"
    assert batch(client, [o]).status_code == 422
    o["purchase"]["authorized_date"] = None
    assert batch(client, [o]).status_code == 200


def test_offset_timestamp_local_day(client):
    prefs(client)
    o = deepcopy(BATCH["observations"][0])
    o.update(time_precision="datetime", occurred_at="2026-10-23T02:00:00Z")
    assert batch(client, [o]).status_code == 200  # October 22 in New York, not UTC date
    n = revision(o, occurred_at="2026-10-23T08:00:00Z")
    assert batch(client, [n]).status_code == 422


def test_source_updates_duplicate_and_consent_not_grant(client):
    u = deepcopy(BATCH["source_updates"][0])
    assert batch(client, [], source_updates=[u]).json()["error"]["code"] == "consent_required"
    prefs(client)
    before = state(client)
    assert batch(client, [], source_updates=[u, u]).status_code == 422
    assert state(client) == before


def test_source_stale_previous_local_day(client):
    prefs(client)
    u = deepcopy(BATCH["source_updates"][0])
    u["attempted_at"] = "2026-11-12T12:00:00Z"
    s = batch(client, [], source_updates=[u]).json()["state"]
    assert s["sources"][0]["status"] == "stale"
    assert s["ledger"][1]["recorded_steps"] is None


def test_activity_current_day_excluded_and_seven_completed_days(client):
    prefs(client)
    obs = deepcopy(BATCH["observations"][3:])
    obs[0]["local_date"] = "2026-11-14"
    obs[1]["local_date"] = "2026-11-06"
    s = batch(client, obs).json()["state"]
    assert s["ledger"][1]["recorded_steps"] == 3500
    assert s["ledger"][1]["days_with_records"] == 1


def test_purchase_window_inclusive_twenty_eight_days(client):
    prefs(client)
    obs = deepcopy(BATCH["observations"][:3])
    for o, day in zip(obs, ("2026-10-17", "2026-10-18", "2026-11-14")):
        o["local_date"] = day
        o["purchase"].update(authorized_date=day, posted_date=day)
    s = batch(client, obs).json()["state"]
    assert s["ledger"][0]["purchase_count"] == 2


def test_tombstone_cannot_change_provenance(client):
    seed(client)
    o = revision(BATCH["observations"][0], operation="remove", lifecycle="removed", purchase=None)
    o["provenance"]["record_id"] = "changed"
    assert batch(client, [o]).status_code == 409


def test_correctness_card_dispute_and_explicit_correction(client):
    seed(client)
    s = feedback(client, scope="card", event_ref=None, attribution=None, value="incorrect").json()
    assert s["ledger"][0]["suggestion"]["status"] == "unavailable"
    assert s["observations"] == EXAMPLE["observations"]
    s = feedback(client, scope="card", event_ref=None, attribution=None, value="correct").json()
    assert s["ledger"][0]["suggestion"]["status"] == "eligible"


def test_stop_survives_restart_and_only_preferences_can_unsuppress(tmp_path):
    def app():
        return create_app(tmp_path, replacements={"synthesis": UnavailableSynthesis()}, clock=lambda: NOW)
    with TestClient(app(), base_url="http://127.0.0.1") as c:
        seed(c)
        feedback(c, scope="card", event_ref=None, attribution=None, kind="suppress", value="stop")
    with TestClient(app(), base_url="http://127.0.0.1") as c:
        s = feedback(c, scope="card", event_ref=None, attribution=None, value="correct").json()
        assert s["ledger"][0]["suggestion"]["status"] == "suppressed"
        assert prefs(c, suppressed_scenario_ids=[])["ledger"][0]["suggestion"]["status"] == "eligible"


def test_database_rejects_second_service_writer(tmp_path):
    app = create_app(tmp_path, replacements={"synthesis": UnavailableSynthesis()}, clock=lambda: NOW)
    with TestClient(app, base_url="http://127.0.0.1"):
        with pytest.raises(RuntimeError, match="already open"):
            create_app(tmp_path, replacements={"synthesis": UnavailableSynthesis()})


def test_expired_incoming_observation_boundary(client):
    prefs(client)
    o = deepcopy(BATCH["observations"][3])
    o["local_date"] = (NOW.date() - timedelta(days=89)).isoformat()
    assert batch(client, [o]).status_code == 200
    old = deepcopy(o)
    old.update(id="too-old", local_date=(NOW.date() - timedelta(days=90)).isoformat())
    assert batch(client, [old]).status_code == 422


async def test_periodic_housekeeping_prunes_without_get_mutation(tmp_path):
    clock = [NOW]
    service = build_service(tmp_path, replacements={"synthesis": UnavailableSynthesis()}, clock=lambda: clock[0])
    await service.start()
    s = await service.state()
    common = {"schema_version": "1", "data_epoch": s["data_epoch"]}
    await service.mutate("/preferences", {**common, "request_id": "consent", "preferences": s["preferences"]})
    await service.mutate("/observations", {**common, "request_id": "batch", "observations": BATCH["observations"], "source_updates": []})
    before = await service.state()
    clock[0] += timedelta(days=100)
    assert await service.state() == before
    await service.housekeep()
    after = await service.state()
    assert after["revision"] > before["revision"] and not after["observations"]
    assert await service.state() == after
    await service.close()


async def test_persistent_running_job_visible_before_synthesis_finishes(tmp_path):
    ext = ControlledSynthesis()
    service = build_service(tmp_path, replacements={"synthesis": ext}, clock=lambda: NOW)
    await service.start()
    s = await service.state()
    body = {"schema_version": "1", "request_id": "process", "data_epoch": s["data_epoch"], "synthesis_mode": "scripted"}
    task = asyncio.create_task(service.mutate("/process", body))
    await ext.entered.wait()
    db = sqlite3.connect(tmp_path / "state.sqlite")
    stored = json.loads(db.execute("SELECT body FROM snapshot").fetchone()[0])
    assert stored["state"]["process"]["status"] == "running"
    assert db.execute("SELECT response FROM receipts WHERE request_id='process'").fetchone()[0] is None
    db.close()
    ext.release.set()
    await task
    await service.close()


async def test_failing_scenario_extension_process_retains_previous_ledger(tmp_path):
    class ToggleScenario:
        id, version, status = "scenarios", "1", "available"
        failed = False
        async def handle(self, operation, payload):
            if self.failed:
                raise RuntimeError("Synthetic deterministic evaluation failure")
            return await ScenariosExtension().handle(operation, payload)
    extension = ToggleScenario()
    service = build_service(tmp_path, replacements={"scenarios": extension, "synthesis": UnavailableSynthesis()}, clock=lambda: NOW)
    await service.start()
    before = await service.state()
    extension.failed = True
    s = await service.mutate("/process", {"schema_version": "1", "request_id": "failure", "data_epoch": before["data_epoch"], "synthesis_mode": "disabled"})
    assert s["process"]["status"] == "failed" and s["process"]["ledger_status"] == "failed"
    assert s["ledger"] == before["ledger"]
    assert service.required_failure
    await service.close()


async def test_synthesis_payload_mutation_cannot_mint_evidence(tmp_path):
    class Mutating:
        id, version, status = "synthesis", "1", "available"
        async def handle(self, operation, payload):
            if operation == "clear":
                return {"cleared": True}
            state = payload["state"]
            fake = {"observation_id": "invented", "revision": 1}
            state["ledger"][0]["evidence"] = [fake]
            r = synthesis(payload["mode"], "completed")
            r["drafts"] = [{"id": "invalid", "text": "Invalid", "card_ids": ["purchase_recurrence"], "evidence": [fake]}]
            return r
    service = build_service(tmp_path, replacements={"synthesis": Mutating()}, clock=lambda: NOW)
    await service.start()
    s = await service.state()
    final = await service.mutate("/process", {"schema_version": "1", "request_id": "mutating", "data_epoch": s["data_epoch"], "synthesis_mode": "scripted"})
    assert final["process"]["synthesis"]["reason"] == "invalid_output"
    assert not final["observations"] and not final["ledger"][0]["evidence"]
    await service.close()


def test_internal_failure_envelope_never_exposes_payload_or_traceback(tmp_path, caplog):
    class BrokenFeedback:
        id, version, status = "feedback", "1", "available"
        async def handle(self, operation, payload):
            raise RuntimeError("SENSITIVE_PRIVATE_SENTINEL")
    app = create_app(tmp_path, replacements={"feedback": BrokenFeedback(), "synthesis": UnavailableSynthesis()}, clock=lambda: NOW)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        seed(c)
        before = state(c)
        r = feedback(c)
        assert r.status_code == 500
        validate("error", r.json())
        assert r.json()["error"]["retryable"] is True
        assert "SENSITIVE_PRIVATE_SENTINEL" not in r.text + caplog.text
        assert state(c) == before


async def test_storage_replacement_preserves_fixture_and_idempotency(tmp_path):
    class MemoryPersistence:
        id, version, status = "persistence", "1", "available"
        def __init__(self):
            self.document, self.receipts = {}, {}
        async def handle(self, operation, payload):
            if operation == "load":
                return deepcopy(self.document)
            if operation == "receipt":
                return deepcopy(self.receipts.get((payload["epoch"], payload["route"], payload["request_id"]), {}))
            if operation == "commit":
                self.document = deepcopy(payload["document"])
                if payload["erase_receipts"]:
                    self.receipts = {}
                r = payload.get("receipt")
                if r:
                    self.receipts[(r["epoch"], r["route"], r["request_id"])] = deepcopy(r)
                return {"committed": True}
            return {"closed": True}
    storage = MemoryPersistence()
    service = build_service(tmp_path, replacements={"persistence": storage, "synthesis": UnavailableSynthesis()}, clock=lambda: NOW)
    await service.start()
    s = await service.state()
    body = {"schema_version": "1", "request_id": "seed", "data_epoch": s["data_epoch"], "action": "seed", "fixture_set": "representative-v1", "consent_demo": True}
    seeded = await service.mutate("/demo", body)
    assert seeded["ledger"] == EXAMPLE["ledger"]
    assert await service.mutate("/demo", body) == seeded
    assert not list(tmp_path.glob("*.sqlite"))
    await service.close()


async def test_concurrent_http_process_allows_preferences_and_get_state(tmp_path):
    ext = ControlledSynthesis()
    app = create_app(tmp_path, replacements={"synthesis": ext}, clock=lambda: NOW)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            s = (await c.get("/state")).json()
            common = {"schema_version": "1", "data_epoch": s["data_epoch"]}
            task = asyncio.create_task(c.post("/process", json={**common, "request_id": "run", "synthesis_mode": "scripted"}))
            await ext.entered.wait()
            s = (await c.get("/state")).json()
            assert s["process"]["status"] == "running"
            s["preferences"]["food_suggestions"] = False
            r = await c.put("/preferences", json={**common, "request_id": "prefs", "preferences": s["preferences"]})
            assert r.status_code == 200
            r = await c.post("/process", json={**common, "request_id": "busy", "synthesis_mode": "scripted"})
            assert r.status_code == 409 and r.json()["error"]["retryable"] is True
            ext.release.set()
            r = await task
            assert r.status_code == 200 and r.json()["process"]["synthesis"]["reason"] == "stale_context"
