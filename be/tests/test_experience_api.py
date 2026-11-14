"""Experience profile, atomic answer persistence and internal observation-backed scenarios."""
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from companion.bootstrap import UnavailableSynthesis
from companion.contracts import validate
from sample_data import BATCH
from companion.domain import iso
from companion.experience_contracts import validate_experience
from companion.extensions.experience import ExperienceExtension
from companion.extensions.persistence import PersistenceExtension
from sample_data import create_app
from test_backend import NOW, batch, feedback, prefs, request, revision, state
from test_plaid import Provider, page, transaction
from companion.connections.plaid import PlaidSandboxClient, SandboxCredentials
from companion.extensions.plaid import PlaidExtension
import httpx


def app_for(path, **changes):
    return create_app(path, experience_profile=True, replacements=changes.pop("replacements", {"synthesis": UnavailableSynthesis()}), clock=changes.pop("clock", lambda: NOW), **changes)


@pytest.fixture
def client(tmp_path):
    with TestClient(app_for(tmp_path), base_url="http://127.0.0.1") as client:
        yield client


def experience(client):
    result = client.get("/experience/v1")
    assert result.status_code == 200, result.text
    validate_experience("experience", result.json())
    return result.json()


def answer_body(value, answer="yes", **extra):
    return {"schema_version": "1", "request_id": str(uuid4()), "data_epoch": value["data_epoch"],
        "expected_revision": value["revision"], "clarification_key": value["clarification"]["key"], "answer": answer, **extra}


def answer(client, value=None, choice="yes", **extra):
    return client.post("/experience/v1/answers", json=answer_body(value or experience(client), choice, **extra))


def internal_fixture(client, scenario="recurring_restaurants"):
    """Test-only setup via ordinary consent/admission, never a new HTTP demo endpoint."""
    prefs(client, enabled_source_ids=["demo"], movement_restriction=False, food_suggestions=True,
          activity_suggestions=True, no_kitchen=False, no_spending=False, suppressed_scenario_ids=[])
    rows = deepcopy(BATCH["observations"])
    for row in rows:
        if row["kind"] == "purchase": row["household_attribution"] = "unknown"
    if scenario == "recurring_restaurants": rows = rows[:3]
    elif scenario == "no_flags": rows = rows[:2] + rows[3:]
    elif scenario == "insufficient_records": rows = []
    result = batch(client, rows, source_updates=deepcopy(BATCH["source_updates"]))
    assert result.status_code == 200, result.text
    if scenario == "answer_correction":
        assert answer(client, choice="no").status_code == 200
    return experience(client)


@pytest.mark.parametrize("enabled,count", [(False, 8), (True, 17)])
def test_explicit_profile_preserves_base_routes_state_and_health(tmp_path, enabled, count):
    with TestClient(create_app(tmp_path, experience_profile=enabled, replacements={"synthesis": UnavailableSynthesis()}, clock=lambda: NOW), base_url="http://127.0.0.1") as client:
        spec = client.get("/openapi.json").json()
        assert len(spec["paths"]) == count
        assert client.get("/experience/v1").status_code == (200 if enabled else 404)
        assert client.post("/experience/v1/demo", json={}).status_code == 404
        validate("state", state(client))
        health = client.get("/health")
        assert health.status_code == 200
        validate("health", health.json())
        assert len(health.json()["capabilities"]) == 8
        assert client.get("/docs").status_code == 200


@pytest.mark.parametrize("scenario,status,counts", [
    ("recurring_restaurants", "pattern", 3), ("mixed_activity", "pattern", 6),
    ("no_flags", "no_flags", 5), ("insufficient_records", "insufficient", 0),
    ("answer_correction", "corrected", 6)])
def test_internal_observation_backed_scenarios_no_canned_screen(client, scenario, status, counts):
    value = internal_fixture(client, scenario)
    assert value["home"]["status"] == status
    assert len(state(client)["observations"]) == counts
    assert value["is_demo"] == bool(counts)
    if counts:
        assert value["provenance"] == "synthetic"
        assert value["visit"]["brief"].startswith("DEMO")
    if status == "no_flags":
        assert value["home"]["headline"] == "No patterns flagged in available records"
        assert len(value["visit"]["observed_information"]) == 2
        assert "healthy" not in json.dumps(value).lower()
        assert "partial or unknown" in value["visit"]["brief"]
    if scenario == "mixed_activity":
        assert "13800 recorded steps" in value["visit"]["observed_information"][1]
    if status == "insufficient":
        assert value["clarification"] is None
        assert not value["visit"]["observed_information"]


@pytest.mark.parametrize("choice", ["yes", "no", "not_sure", "dismissed"])
def test_answer_persists_restart_does_not_mutate_facts_or_poll_identity(tmp_path, choice):
    with TestClient(app_for(tmp_path), base_url="http://127.0.0.1") as client:
        value = internal_fixture(client, "mixed_activity")
        before = state(client)
        result = answer(client, value, choice)
        assert result.status_code == 200, result.text
        saved = result.json()
        assert saved["revision"] == value["revision"] + 1
        assert saved["clarification"]["answer"] == choice
        assert not saved["clarification"]["should_prompt"]
        after = state(client)
        assert after["observations"] == before["observations"]
        assert after["ledger"] == before["ledger"]
        for _ in range(3): assert experience(client) == saved
    with TestClient(app_for(tmp_path), base_url="http://127.0.0.1") as client:
        restored = experience(client)
        assert restored["clarification"] == saved["clarification"]
        assert restored["visit"] == saved["visit"]
        assert "experience_answers" not in json.dumps(state(client))


def test_answer_idempotency_conflict_and_stale_revision(client):
    value = internal_fixture(client)
    body = answer_body(value)
    first = client.post("/experience/v1/answers", json=body)
    assert first.status_code == 200
    second = client.post("/experience/v1/answers", json=body)
    assert second.json() == first.json()
    conflict = client.post("/experience/v1/answers", json={**body, "answer": "no"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    stale = answer(client, value)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_experience"


def test_obsolete_pattern_rejected_server_side_even_with_current_revision(client):
    old = internal_fixture(client)
    row = state(client)["observations"][0]
    changed = revision(row, household_attribution="self")
    assert batch(client, [changed]).status_code == 200
    current = experience(client)
    rejected = answer(client, current, clarification_key=old["clarification"]["key"])
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "obsolete_pattern"
    assert experience(client)["clarification"]["answer"] is None


def test_material_revision_cooldown_and_seven_day_cadence(tmp_path):
    clock = [NOW]
    with TestClient(app_for(tmp_path, clock=lambda: clock[0]), base_url="http://127.0.0.1") as client:
        value = internal_fixture(client)
        assert answer(client, value, "dismissed").status_code == 200
        row = state(client)["observations"][0]
        assert batch(client, [revision(row, household_attribution="self")]).status_code == 200
        current = experience(client)
        assert current["clarification"]["answer"] is None
        assert not current["clarification"]["should_prompt"]
        assert current["clarification"]["next_prompt_at"] == iso(NOW + timedelta(days=7))
        # Voluntary correction is still possible during automatic-prompt cooldown.
        assert current["clarification"]["can_answer"]


def test_answer_correction_uses_same_key_and_keeps_observed_counts(client):
    initial = internal_fixture(client)
    first = answer(client, initial, "yes").json()
    corrected = answer(client, first, "no")
    assert corrected.status_code == 200
    assert corrected.json()["clarification"]["key"] == initial["clarification"]["key"]
    assert corrected.json()["visit"]["observed_information"] == initial["visit"]["observed_information"]
    assert corrected.json()["home"]["status"] == "corrected"
    assert not corrected.json()["visit"]["inferred_habits"]


def test_consent_revocation_removes_answers_and_receipts_without_revival(client, tmp_path):
    value = internal_fixture(client)
    body = answer_body(value)
    assert client.post("/experience/v1/answers", json=body).status_code == 200
    prefs(client, enabled_source_ids=[])
    current = experience(client)
    assert current["clarification"] is None
    assert current["visit"]["confirmed_context"] == []
    replay = client.post("/experience/v1/answers", json=body)
    assert replay.status_code == 409
    db = sqlite3.connect(tmp_path / "state.sqlite")
    document = json.loads(db.execute("SELECT body FROM snapshot").fetchone()[0])
    assert document["meta"]["experience_answers"] == []
    assert db.execute("SELECT COUNT(*) FROM receipts WHERE route='/experience/v1/answers'").fetchone()[0] == 0
    db.close()
    prefs(client, enabled_source_ids=["demo"])
    restored = experience(client)
    assert restored["clarification"]["answer"] is None
    assert restored["visit"]["confirmed_context"] == []


def test_deletion_rotates_epoch_and_never_replays_answer(client):
    value = internal_fixture(client)
    body = answer_body(value)
    assert client.post("/experience/v1/answers", json=body).status_code == 200
    assert request(client, "/data", "delete", confirm="delete_all").status_code == 200
    after = experience(client)
    assert after["data_epoch"] != value["data_epoch"]
    assert after["clarification"] is None and after["visit"]["confirmed_context"] == []
    stale = client.post("/experience/v1/answers", json=body)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "epoch_mismatch"


def test_immediate_food_suppression_no_prompt_and_no_false_all_clear(client):
    value = internal_fixture(client)
    prefs(client, food_suggestions=False)
    after = experience(client)
    assert after["clarification"] is None
    assert after["home"]["status"] == "suppressed"
    assert not after["visit"]["inferred_habits"]
    rejected = answer(client, {**value, "revision": after["revision"]})
    assert rejected.status_code == 409


@pytest.mark.parametrize("field", ["confirmed_concern", "confirmed_eating_context", "confirmed_activity_context", "user_id", "evaluation_date", "brief", "created_at"])
def test_legacy_local_context_or_identity_cannot_be_forged_through_answer(client, field):
    value = internal_fixture(client)
    result = answer(client, value, **{field: "PRIVATE_FORGED"})
    assert result.status_code == 422
    assert "PRIVATE_FORGED" not in result.text
    assert not experience(client)["visit"]["confirmed_context"]


@pytest.mark.parametrize("body,status", [("{", 400), ('{"schema_version":"1","schema_version":"1"}', 400), ("null", 422), ("NaN", 400), ("[1]", 422), ("x" * (1024 * 1024 + 1), 413)])
def test_malformed_duplicate_oversized_json_is_sanitized(client, body, status):
    result = client.post("/experience/v1/answers", content=body, headers={"content-type": "application/json"})
    assert result.status_code == status
    validate_experience("error", result.json())


def test_no_query_or_get_body_and_openapi_complete_shapes(client):
    assert client.get("/experience/v1?user_id=forged").status_code == 422
    assert client.request("GET", "/experience/v1", content="{}").status_code == 422
    spec = client.get("/openapi.json").json()
    schema = spec["components"]["schemas"]["experience_answer_request"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"schema_version", "request_id", "data_epoch", "expected_revision", "clarification_key", "answer"}
    assert set(schema["properties"]["answer"]["enum"]) == {"yes", "no", "not_sure", "dismissed"}
    assert "409" in spec["paths"]["/experience/v1/answers"]["post"]["responses"]


def test_extension_is_replaceable_via_public_registry(tmp_path):
    class Replacement:
        id, version, status = "experience", "1", "available"
        def __init__(self): self.calls = []; self.delegate = ExperienceExtension()
        async def handle(self, operation, payload):
            self.calls.append(operation)
            return await self.delegate.handle(operation, payload)
    extension = Replacement()
    with TestClient(app_for(tmp_path, replacements={"synthesis": UnavailableSynthesis(), "experience": extension}), base_url="http://127.0.0.1") as client:
        internal_fixture(client)
        assert answer(client).status_code == 200
        assert {"project", "answer", "clean"} <= set(extension.calls)


def test_answer_transaction_failure_does_not_publish_or_cache(tmp_path):
    class FailingPersistence(PersistenceExtension):
        fail = False
        async def handle(self, operation, payload):
            if self.fail and operation == "commit" and payload.get("receipt", {}).get("route") == "/experience/v1/answers":
                self.fail = False
                raise RuntimeError("PRIVATE_DATABASE_ERROR")
            return await super().handle(operation, payload)
    storage = FailingPersistence(tmp_path / "state.sqlite")
    with TestClient(app_for(tmp_path, replacements={"synthesis": UnavailableSynthesis(), "persistence": storage}), base_url="http://127.0.0.1") as client:
        value = internal_fixture(client)
        body = answer_body(value)
        storage.fail = True
        result = client.post("/experience/v1/answers", json=body)
        assert result.status_code == 500 and "PRIVATE_DATABASE_ERROR" not in result.text
        assert experience(client) == value
        retry = client.post("/experience/v1/answers", json=body)
        assert retry.status_code == 200


def test_instances_do_not_share_answers(tmp_path):
    with TestClient(app_for(tmp_path / "a"), base_url="http://127.0.0.1") as a, TestClient(app_for(tmp_path / "b"), base_url="http://127.0.0.1") as b:
        va, vb = internal_fixture(a), internal_fixture(b)
        assert va["data_epoch"] != vb["data_epoch"]
        assert answer(a, va, "no").status_code == 200
        assert experience(a)["home"]["status"] == "corrected"
        assert experience(b)["home"]["status"] == "pattern"
        assert experience(b)["clarification"]["answer"] is None
        foreign = b.post("/experience/v1/answers", json=answer_body(va))
        assert foreign.status_code == 409


def test_retention_prunes_answers_and_keeps_no_false_all_clear(tmp_path):
    clock = [NOW]
    with TestClient(app_for(tmp_path, clock=lambda: clock[0]), base_url="http://127.0.0.1") as client:
        value = internal_fixture(client)
        assert answer(client, value).status_code == 200
        clock[0] += timedelta(days=91)
        after = experience(client)
        assert after["clarification"] is None
        assert after["visit"]["confirmed_context"] == []
        assert not state(client)["observations"]
        assert after["home"]["status"] != "no_flags"


def test_plaid_composition_selects_actual_connection_never_sums_demo(tmp_path):
    provider = Provider()
    rows = [transaction("plaid-" + str(i), date=d, authorized_date=d) for i, d in enumerate(("2026-10-22", "2026-11-02", "2026-11-10"))]
    provider.pages = [page(rows)]
    extension = PlaidExtension(tmp_path, PlaidSandboxClient(SandboxCredentials("test", "test"), httpx.MockTransport(provider.handle)))
    app = app_for(tmp_path, plaid_profile=True, replacements={"synthesis": UnavailableSynthesis(), "plaid": extension})
    with TestClient(app, base_url="http://127.0.0.1") as client:
        internal_fixture(client)
        assert len(client.get("/openapi.json").json()["paths"]) == 18
        assert request(client, "/connections/plaid", action="link_token", consent_sandbox=True).status_code == 200
        r = request(client, "/connections/plaid", action="exchange", public_token="public-sandbox-test")
        assert r.status_code == 200, r.text
        r = request(client, "/connections/plaid", action="sync")
        assert r.status_code == 200, r.text
        result = experience(client)
        assert result["concern"]["evidence_count"] == 3
        from companion.plaid_contracts import validate_profile
        current_state = client.get("/state").json()
        validate_profile("state", current_state)
        assert len(current_state["observations"]) == 6
        assert "PRIVATE" not in json.dumps(result)
        assert result["clarification"]
        assert answer(client, result, "yes").status_code == 200
        r = request(client, "/connections/plaid", epoch=current_state["data_epoch"], action="disconnect")
        assert r.status_code == 200, r.text
        assert experience(client)["visit"]["confirmed_context"] == []


def test_concurrent_answers_publish_exactly_one_revision(client):
    from concurrent.futures import ThreadPoolExecutor
    initial = internal_fixture(client)
    bodies = [answer_body(initial, choice) for choice in ("yes", "no")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda body: client.post("/experience/v1/answers", json=body), bodies))
    assert sorted(r.status_code for r in results) == [200, 409]
    winner = next(r.json() for r in results if r.status_code == 200)
    assert experience(client) == winner
    assert winner["revision"] == initial["revision"] + 1


def test_revoking_different_source_erases_cached_brief_not_valid_purchase_answer(client, tmp_path):
    value = internal_fixture(client)
    body = answer_body(value)
    assert client.post("/experience/v1/answers", json=body).status_code == 200
    # Permission changes in any source invalidate brief receipts, even if the
    # answer itself still refers only to the unaffected purchase source.
    prefs(client, enabled_source_ids=["demo", "healthkit"])
    prefs(client, enabled_source_ids=["demo"])
    current = experience(client)
    assert current["clarification"]["answer"] == "yes"
    stale = client.post("/experience/v1/answers", json=body)
    assert stale.status_code == 409
    db = sqlite3.connect(tmp_path / "state.sqlite")
    assert db.execute("SELECT COUNT(*) FROM receipts WHERE route='/experience/v1/answers'").fetchone()[0] == 0
    db.close()


def test_internal_fixture_helpers_never_change_active_provider_when_unused(tmp_path):
    provider = Provider()
    extension = PlaidExtension(tmp_path, PlaidSandboxClient(SandboxCredentials("test", "test"), httpx.MockTransport(provider.handle)))
    app = app_for(tmp_path, plaid_profile=True, replacements={"synthesis": UnavailableSynthesis(), "plaid": extension})
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/experience/v1/demo", json={"scenario": "recurring_restaurants"}).status_code == 404
        assert experience(client)["provenance"] == "none"
        assert client.get("/state").json()["observations"] == []
