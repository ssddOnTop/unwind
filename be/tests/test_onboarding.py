"""Local onboarding tests: scratch SQLite, mock SQL and disposable PostgreSQL.

No external provider, real Clerk account, live database, model or native API is used.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from jsonschema import Draft202012Validator

from companion.bootstrap import UnavailableSynthesis
from companion.contracts import DomainError
from companion.domain import defaults
from companion.http import create_app
from companion.onboarding import (META_KEY, Onboarding, OnboardingAction, act,
                                  available_records, get, initialize)
from sample_data import BATCH
from test_auth_http import Harness, key, raw_call
from test_postgres import local_pg_cluster, pg_dsn, runtime, account as pg_account
from test_patterns import configured as pattern_records


NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


def application(path):
    return create_app(path, replacements={"synthesis": UnavailableSynthesis()},
                      clock=lambda: NOW, housekeeping_interval=3600)


@pytest.fixture
async def app(tmp_path):
    app = application(tmp_path)
    await app.state.service.start()
    try:
        yield app
    finally:
        await app.state.service.close()


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://127.0.0.1") as client:
        yield client


def action(view, action="skip", request_id=None, **overrides):
    return OnboardingAction.model_validate({
        "schema_version": "1", "request_id": request_id or f"decision-{view['revision']}",
        "data_epoch": view["data_epoch"], "expected_revision": view["revision"],
        "step": view["current_step"], "action": action, **overrides,
    })


async def to_sync(service):
    view = await get(service)
    for step in ("health", "bank", "notifications"):
        assert view["current_step"] == step
        view = await act(service, action(view))
    assert view["current_step"] == "initial_sync"
    return view


def assert_domain(error, code):
    assert error.value.code == code
    assert error.value.status == 409


class PatternRecords:
    """Mock the existing scoped transaction, including mandatory account parameters."""
    def __init__(self, count=0, *, historical=False, failure=None, account="account-a", tables=(True, True, True)):
        self.store = SimpleNamespace(account_id=account)
        self.count, self.historical, self.failure = count, historical, failure
        self.queries, self.tables = [], tables

    @asynccontextmanager
    async def transaction(self):
        if self.failure:
            raise self.failure
        yield self

    async def execute(self, sql, params=()):
        if "to_regclass" in sql:
            assert params == ()
        else:
            assert params == (self.store.account_id, self.store.account_id)
            assert "account_id=%s" in sql
        assert "health_samples" in sql and "pattern_transactions" in sql
        self.queries.append(sql)
        return self

    async def fetchone(self):
        if "to_regclass" in self.queries[-1]:
            return self.tables
        return (self.historical or self.count > 0,) if "EXISTS" in self.queries[-1] else (self.count,)


async def test_empty_account_has_typed_optional_sequential_steps(app):
    service = app.state.service
    before = deepcopy(service.document)
    view = await get(service)
    assert Onboarding.model_validate(view).model_dump() == view
    assert view["status"] == "active" and view["current_step"] == "health"
    assert not view["legacy"] and view["revision"] == 0
    assert [s["id"] for s in view["steps"]] == ["health", "bank", "notifications"]
    assert all(s["optional"] and s["purpose"] and s["decision"] == "pending" for s in view["steps"])
    assert "Sandbox" in view["steps"][1]["title"]
    assert view["initial_sync"] == {"status": "pending", "available_records": None}
    assert service.document == before  # reads do not rewrite established progress
    assert len(service.document["meta"][META_KEY]["receipts"]) == 0


@pytest.mark.parametrize("decision", ["continue", "skip", "denied", "unavailable"])
async def test_each_explicit_decision_advances_without_permission_inference(app, decision):
    service = app.state.service
    state = await service.state()
    view = await get(service)
    for step in ("health", "bank", "notifications"):
        view = await act(service, action(view, decision))
        assert next(s for s in view["steps"] if s["id"] == step)["decision"] == decision
        assert await service.state() == state
    assert view["current_step"] == "initial_sync"
    view = await act(service, action(view, "finish"))
    assert view["status"] == "complete" and not view["legacy"]
    assert view["initial_sync"] == {"status": "no_data", "available_records": 0}
    assert await service.state() == state
    assert len(service.document["meta"][META_KEY]["receipts"]) == 4


@pytest.mark.parametrize("evidence", ["preferences", "granted", "denied", "revoked", "bank", "bank_items", "records"])
async def test_legacy_bootstrap_preserves_existing_user_state(app, evidence):
    service = app.state.service
    doc = deepcopy(service.document)
    doc["meta"].pop(META_KEY)
    if evidence == "preferences":
        doc["state"]["preferences"]["enabled_source_ids"] = ["healthkit"]
    elif evidence in {"granted", "denied", "revoked"}:
        next(s for s in doc["state"]["sources"] if s["id"] == "healthkit")["consent"] = evidence
    elif evidence == "bank":
        doc["meta"]["plaid_connected"] = True
    elif evidence == "bank_items":
        doc["meta"]["banking_item_contexts"] = [{"item_id": "private-item", "generation": 2}]
    else:
        doc["state"]["observations"] = [{"source_id": "healthkit"}]
    before = deepcopy(doc)
    await initialize(service, doc)
    view = doc["meta"].pop(META_KEY)["view"]
    assert doc == before
    assert view["legacy"] and view["status"] == "complete"
    assert view["current_step"] == "complete"
    assert all(s["decision"] == "pending" for s in view["steps"])
    assert view["initial_sync"] == {"status": "deferred", "available_records": None}
    assert "private-item" not in str(view)


@pytest.mark.parametrize("noise", ["revision", "capabilities", "vault", "demo", "read_access"])
async def test_no_legacy_inference_from_empty_runtime_metadata(app, noise):
    service = app.state.service
    doc = {"state": defaults(NOW), "meta": {}}
    if noise == "revision":
        doc["state"]["revision"] = 750
    elif noise == "capabilities":
        for s in doc["state"]["sources"]:
            s.update(capability="available", status="ready")
    elif noise == "vault":
        doc["meta"].update(banking_vault="encrypted-empty-vault", plaid_vault="empty", banking_item_contexts=[])
    elif noise == "demo":
        doc["state"]["preferences"]["enabled_source_ids"] = ["demo"]
        doc["state"]["sources"][0].update(enabled=True, consent="granted")
        doc["state"]["observations"] = [{"source_id": "demo"}]
    else:
        next(s for s in doc["state"]["sources"] if s["id"] == "healthkit").update(
            authorization_requested=True, read_access="samples_readable")
    await initialize(service, doc)
    assert doc["meta"][META_KEY]["view"]["status"] == "active"


@pytest.mark.parametrize("historical", [False, True])
async def test_bootstrap_checks_account_scoped_stored_records(app, historical):
    service = app.state.service
    service.pattern_coordinator = records = PatternRecords(historical=historical)
    doc = {"state": defaults(NOW), "meta": {}}
    await initialize(service, doc)
    assert doc["meta"][META_KEY]["view"]["legacy"] == historical
    assert len(records.queries) == 2
    assert "b.active" not in records.queries[-1]  # historical records count for prior use


async def test_partial_pattern_schema_is_not_misclassified_as_empty(app):
    service = app.state.service
    service.pattern_coordinator = PatternRecords(tables=(True, False, True))
    doc = {"state": defaults(NOW), "meta": {}}
    with pytest.raises(DomainError) as exc:
        await initialize(service, doc)
    assert exc.value.code == "internal_error" and exc.value.status == 503
    assert META_KEY not in doc["meta"]
    sync = await to_sync(service)
    before = deepcopy(service.document)
    with pytest.raises(DomainError) as exc:
        await act(service, action(sync, "finish"))
    assert exc.value.code == "internal_error" and exc.value.status == 503
    assert service.document == before


async def test_legacy_lookup_failure_never_misclassifies_as_empty(app):
    service = app.state.service
    service.pattern_coordinator = PatternRecords(failure=RuntimeError("private db error"))
    doc = {"state": defaults(NOW), "meta": {}}
    with pytest.raises(RuntimeError):
        await initialize(service, doc)
    assert META_KEY not in doc["meta"]


async def test_startup_classifies_legacy_before_retention_and_persists_it(tmp_path):
    app = application(tmp_path)
    service = app.state.service
    doc = {"state": defaults(NOW), "meta": {"issued": [], "fixture_session": False}}
    old = deepcopy(BATCH["observations"][0])
    old.update(source_id="healthkit", local_date="2020-01-01")
    doc["state"]["observations"] = [old]
    # Seed a pre-onboarding snapshot using the local persistence adapter.
    await service.registry.dispatch("persistence", "commit", {"document": doc})
    await service.start()
    assert service.document["state"]["observations"] == []
    assert (await get(service))["legacy"]
    await service.close()
    restarted = application(tmp_path).state.service
    await restarted.start()
    try:
        assert (await get(restarted))["legacy"]
    finally:
        await restarted.close()


async def test_new_accounts_not_reclassified_after_preference_or_upload_changes(app):
    service = app.state.service
    view = await get(service)
    doc = deepcopy(service.document)
    next(s for s in doc["state"]["sources"] if s["id"] == "healthkit").update(enabled=True, consent="granted")
    doc["state"]["preferences"]["enabled_source_ids"] = ["healthkit"]
    await service.commit(doc)
    assert (await get(service)) == view  # independent of the main state revision
    advanced = await act(service, action(view, "continue"))
    assert advanced["current_step"] == "bank" and not advanced["legacy"]


async def test_initial_sync_derives_records_only_from_server(app):
    service = app.state.service
    view = await to_sync(service)
    service.pattern_coordinator = records = PatternRecords(count=7)
    result = await act(service, action(view, "finish"))
    assert result["initial_sync"] == {"status": "records_available", "available_records": 7}
    assert "b.active" in records.queries[-1] and "NOT h.deleted" in records.queries[-1]
    assert "190 days" in records.queries[-1]
    assert all(s["consent"] == "not_requested" for s in service.document["state"]["sources"])


async def test_deferred_is_not_fabricated_upload_success(app, monkeypatch):
    service = app.state.service
    view = await to_sync(service)

    async def must_not_read(*args):
        raise AssertionError("Explicit defer must not inspect stored records")

    monkeypatch.setattr("companion.onboarding.available_records", must_not_read)
    result = await act(service, action(view, "defer"))
    assert result["status"] == "complete"
    assert result["initial_sync"] == {"status": "deferred", "available_records": None}
    assert service.document["meta"][META_KEY]["receipts"]["decision-3"]["response"] == result


async def test_failed_finish_preserves_progress_and_receipt_for_retry(app):
    service = app.state.service
    view = await to_sync(service)
    before = deepcopy(service.document)
    request = action(view, "finish")
    service.pattern_coordinator = records = PatternRecords(failure=RuntimeError("private storage details"))
    with pytest.raises(DomainError) as exc:
        await act(service, request)
    assert exc.value.code == "internal_error" and exc.value.status == 503
    assert "private storage" not in exc.value.message
    assert service.document == before
    assert await service.registry.dispatch("persistence", "load", {}) == before
    records.failure, records.count = None, 3
    result = await act(service, request)
    assert result["initial_sync"] == {"status": "records_available", "available_records": 3}
    assert await act(service, request) == result


async def test_api_failed_finish_is_sanitized_retryable_and_same_request_recovers(client, app):
    service = app.state.service
    view = await to_sync(service)
    request = action(view, "finish").model_dump()
    before = deepcopy(service.document)
    service.pattern_coordinator = records = PatternRecords(failure=RuntimeError("secret provider details"))
    response = await client.post("/onboarding/v1/actions", json=request)
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "internal_error", "retryable": True, "request_id": "decision-3", "details": [],
        "message": "Record availability could not be checked. Retry this request.",
    }
    assert "secret" not in response.text
    assert service.document == before
    assert await service.registry.dispatch("persistence", "load", {}) == before
    assert (await client.get("/onboarding/v1")).json() == view
    spec = (await client.get("/openapi.json")).json()
    schema = spec["paths"]["/onboarding/v1/actions"]["post"]["responses"]["503"]["content"]["application/json"]["schema"]
    Draft202012Validator({**schema, "components": spec["components"]}).validate(response.json())
    records.failure, records.count = None, 5
    recovered = await client.post("/onboarding/v1/actions", json=request)
    assert recovered.status_code == 200
    assert recovered.json()["initial_sync"] == {"status": "records_available", "available_records": 5}
    assert recovered.json()["revision"] == view["revision"] + 1
    records.failure = AssertionError("A successful replay must not recheck records")
    assert (await client.post("/onboarding/v1/actions", json=request)).json() == recovered.json()


async def test_epoch_revision_order_and_request_id_fences(app):
    service = app.state.service
    initial = await get(service)
    with pytest.raises(DomainError) as exc:
        await act(service, action(initial, step="bank"))
    assert_domain(exc, "invalid_request")
    with pytest.raises(DomainError) as exc:
        await act(service, action(initial, data_epoch="different-account-epoch"))
    assert_domain(exc, "epoch_mismatch")
    request = action(initial)
    result = await act(service, request)
    with pytest.raises(DomainError) as exc:
        await act(service, action(initial, request_id="other"))
    assert_domain(exc, "stale_onboarding")
    with pytest.raises(DomainError) as exc:
        await act(service, action(initial, "denied"))
    assert_domain(exc, "idempotency_conflict")
    assert await act(service, request) == result
    assert await get(service) == result


async def test_concurrent_decisions_have_single_winner_and_exact_replay(app):
    service = app.state.service
    initial = await get(service)
    outcomes = await asyncio.gather(act(service, action(initial)), act(service, action(initial)))
    assert outcomes[0] == outcomes[1] and outcomes[0]["revision"] == 1
    current = await get(service)
    outcomes = await asyncio.gather(act(service, action(current, request_id="a")),
                                    act(service, action(current, request_id="b")), return_exceptions=True)
    assert sum(isinstance(o, DomainError) for o in outcomes) == 1
    assert (await get(service))["revision"] == 2


async def test_resume_replay_complete_and_no_repeat_across_restarts(tmp_path):
    service = application(tmp_path).state.service
    await service.start()
    requests, responses = [], []
    for decision in ("continue", "denied", "unavailable", "finish"):
        request = action(await get(service), decision)
        result = await act(service, request)
        requests.append(request)
        responses.append(result)
        await service.close()
        service = application(tmp_path).state.service
        await service.start()
        assert await get(service) == result
        assert await act(service, request) == result
    try:
        assert (await get(service))["status"] == "complete"
        for request, response in zip(requests, responses):
            assert await act(service, request) == response
        with pytest.raises(DomainError) as exc:
            await act(service, action(responses[-1], "finish", request_id="repeat", step="initial_sync"))
        assert_domain(exc, "onboarding_complete")
        assert len(service.document["meta"][META_KEY]["receipts"]) == 4
    finally:
        await service.close()


@pytest.mark.parametrize("failure", [RuntimeError("synthetic commit failure"), asyncio.CancelledError()])
async def test_commit_failure_or_interruption_rolls_back_progress_and_receipt(app, monkeypatch, failure):
    service = app.state.service
    initial = await get(service)
    before = deepcopy(service.document)
    request = action(initial)
    dispatch = service.registry.dispatch

    async def fail(capability, operation, payload):
        if capability == "persistence" and operation == "commit":
            raise failure
        return await dispatch(capability, operation, payload)

    with monkeypatch.context() as patch:
        patch.setattr(service.registry, "dispatch", fail)
        with pytest.raises(type(failure)):
            await act(service, request)
    assert service.document == before
    assert await service.registry.dispatch("persistence", "load", {}) == before
    assert (await act(service, request))["revision"] == 1


async def test_cancellation_during_record_read_does_not_complete(app):
    service = app.state.service
    view = await to_sync(service)
    service.pattern_coordinator = PatternRecords(failure=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await act(service, action(view, "finish"))
    assert await get(service) == view


async def test_old_receipts_cannot_replay_after_data_erasure(app):
    service = app.state.service
    initial = await get(service)
    request = action(initial)
    await act(service, request)
    await service.mutate("/data", {"schema_version": "1", "request_id": "delete", "data_epoch": initial["data_epoch"]})
    refreshed = await get(service)
    assert refreshed["data_epoch"] != initial["data_epoch"]
    assert refreshed["current_step"] == "health"
    with pytest.raises(DomainError) as exc:
        await act(service, request)
    assert_domain(exc, "epoch_mismatch")


async def test_onboarding_journal_survives_generic_receipt_erasure(app):
    service = app.state.service
    request = action(await get(service))
    result = await act(service, request)
    await service.commit(deepcopy(service.document), erase=True, bump=False)
    assert await act(service, request) == result


async def test_normalized_postgres_records_bootstrap_legacy_per_account(app, pattern_records):
    from companion.extensions.postgres import PostgresPersistenceExtension
    from companion.pattern_store import PatternCoordinator
    runtime, account_a, account_b, store, coordinator, model = pattern_records
    service = app.state.service
    service.pattern_coordinator = coordinator
    legacy = {"state": defaults(NOW), "meta": {}}
    await initialize(service, legacy)
    assert legacy["meta"][META_KEY]["view"]["legacy"]
    assert await available_records(service, legacy) > 0
    other_store = PostgresPersistenceExtension(runtime, account_b)
    service.pattern_coordinator = PatternCoordinator(service, other_store)
    try:
        empty = {"state": defaults(NOW), "meta": {}}
        await initialize(service, empty)
        assert empty["meta"][META_KEY]["view"]["current_step"] == "health"
        assert not empty["meta"][META_KEY]["view"]["legacy"]
        assert await available_records(service, empty) == 0
        assert model.calls == []
    finally:
        await other_store.handle("close", {})


async def test_postgres_startup_without_optional_pattern_tables(runtime, tmp_path):
    from companion.extensions.postgres import PostgresPersistenceExtension
    from companion.pattern_store import PatternCoordinator
    account_id = await pg_account(runtime)
    store = PostgresPersistenceExtension(runtime, account_id)
    app = create_app(tmp_path, replacements={"persistence": store, "synthesis": UnavailableSynthesis()},
                     clock=lambda: NOW)
    service = app.state.service
    service.pattern_coordinator = PatternCoordinator(service, store)
    try:
        await service.start()
        assert (await get(service))["current_step"] == "health"
        sync = await to_sync(service)
        result = await act(service, action(sync, "finish"))
        assert result["initial_sync"] == {"status": "no_data", "available_records": 0}
        assert (await store.handle("load", {}))["meta"][META_KEY]["view"] == result
    finally:
        await service.close()


async def test_snapshot_record_availability_excludes_demo_removed_and_bank_exclusions(app):
    service = app.state.service
    doc = deepcopy(service.document)
    record = deepcopy(BATCH["observations"][0])
    record.update(id="bank-one", source_id="plaid", mode="live")
    record["provenance"]["adapter"] = "plaid"
    removed = deepcopy(record)
    removed.update(id="bank-two", operation="remove")
    excluded = deepcopy(record)
    excluded.update(id="bank-three")
    doc["state"]["observations"] = [record, removed, excluded, deepcopy(BATCH["observations"][0])]
    doc["meta"]["banking_excluded_ids"] = ["bank-three"]
    assert await available_records(service, doc) == 1


async def test_bootstrap_preserves_public_state_revision_and_timestamp(tmp_path):
    service = application(tmp_path).state.service
    await service.start()
    doc = deepcopy(service.document)
    doc["meta"].pop(META_KEY)
    # A migrated account may have no historical source decisions. It is still
    # eligible for setup, but adding setup metadata must not bump its public state.
    await service.commit(doc, bump=False)
    before = await service.state()
    await service.close()
    restarted = application(tmp_path).state.service
    await restarted.start()
    try:
        assert await restarted.state() == before
        assert (await get(restarted))["current_step"] == "health"
    finally:
        await restarted.close()


async def test_api_conflicts_return_envelopes_and_do_not_advance(client):
    view = (await client.get("/onboarding/v1")).json()
    for overrides, code in (({"data_epoch": "stale"}, "epoch_mismatch"),
                            ({"expected_revision": 1}, "stale_onboarding"),
                            ({"step": "bank"}, "invalid_request")):
        response = await client.post("/onboarding/v1/actions", json=action(view, **overrides).model_dump())
        assert response.status_code == 409
        assert response.json()["error"]["code"] == code
        assert response.json()["error"]["request_id"] == "decision-0"
    assert (await client.get("/onboarding/v1")).json() == view


async def test_api_commit_failure_masks_private_details_and_retry_succeeds(client, app, monkeypatch):
    service = app.state.service
    view = (await client.get("/onboarding/v1")).json()
    dispatch = service.registry.dispatch

    async def fail(capability, operation, payload):
        if capability == "persistence" and operation == "commit":
            raise RuntimeError("private commit details")
        return await dispatch(capability, operation, payload)

    with monkeypatch.context() as patch:
        patch.setattr(service.registry, "dispatch", fail)
        response = await client.post("/onboarding/v1/actions", json=action(view).model_dump())
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert "private commit" not in response.text
    assert (await client.get("/onboarding/v1")).json() == view
    assert (await client.post("/onboarding/v1/actions", json=action(view).model_dump())).status_code == 200


async def test_api_shape_openapi_and_success(client, app):
    response = await client.get("/onboarding/v1")
    assert response.status_code == 200
    view = response.json()
    response = await client.post("/onboarding/v1/actions", json=action(view).model_dump())
    assert response.status_code == 200
    assert response.json()["current_step"] == "bank"
    assert response.json() == await get(app.state.service)
    schema = (await client.get("/openapi.json")).json()
    assert schema["paths"]["/onboarding/v1/actions"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/OnboardingAction")
    assert schema["components"]["schemas"]["OnboardingAction"]["additionalProperties"] is False


@pytest.mark.parametrize("change", [
    {"account_id": "other"}, {"authorization": "granted"}, {"available_records": 99},
    {"schema_version": "2"}, {"expected_revision": True}, {"expected_revision": "0"},
    {"expected_revision": -1}, {"request_id": ""}, {"request_id": "x" * 161},
    {"action": "finish"}, {"step": "initial_sync", "action": "skip"},
])
async def test_api_rejects_identity_permission_record_claims_and_invalid_actions(client, change):
    view = (await client.get("/onboarding/v1")).json()
    body = {**action(view).model_dump(), **change}
    response = await client.post("/onboarding/v1/actions", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert (await client.get("/onboarding/v1")).json() == view


async def test_openapi_documents_onboarding_specific_conflicts_without_widening_legacy(client, app):
    from companion.contracts import SCHEMA
    spec = (await client.get("/openapi.json")).json()
    codes = spec["components"]["schemas"]["OnboardingErrorBody"]["properties"]["code"]["enum"]
    assert {"stale_onboarding", "onboarding_complete", "internal_error"} <= set(codes)
    legacy_codes = SCHEMA["$defs"]["error"]["properties"]["error"]["properties"]["code"]["enum"]
    assert "stale_onboarding" not in legacy_codes and "onboarding_complete" not in legacy_codes
    schema = spec["paths"]["/onboarding/v1/actions"]["post"]["responses"]["409"]["content"]["application/json"]["schema"]
    validator = Draft202012Validator({**schema, "components": spec["components"]})
    view = await get(app.state.service)
    stale = await client.post("/onboarding/v1/actions", json=action(view, expected_revision=1).model_dump())
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_onboarding"
    validator.validate(stale.json())
    sync = await to_sync(app.state.service)
    completed = await act(app.state.service, action(sync, "defer"))
    repeat = action(completed, "finish", request_id="new-after-complete", step="initial_sync")
    response = await client.post("/onboarding/v1/actions", json=repeat.model_dump())
    assert response.status_code == 409 and response.json()["error"]["code"] == "onboarding_complete"
    validator.validate(response.json())


async def test_api_malformed_duplicate_query_body_and_media_boundaries(client):
    for raw in ('{}oops', '{"schema_version":"1","schema_version":"1"}', '{"x":NaN}'):
        response = await client.post("/onboarding/v1/actions", content=raw, headers={"Content-Type": "application/json"})
        assert response.status_code == 400
    assert (await client.get("/onboarding/v1?account_id=other")).status_code == 422
    assert (await client.request("GET", "/onboarding/v1", content="{}")).status_code == 422
    assert (await client.post("/onboarding/v1/actions", content="{}", headers={"Content-Type": "text/plain"})).status_code == 422
    assert (await client.post("/onboarding/v1/actions", content="x" * (1024 * 1024 + 1))).status_code == 413


async def test_authenticated_gateway_isolates_accounts_and_rejects_untrusted_requests(tmp_path, key):
    harness = Harness(tmp_path, key)
    async with harness.client() as client:
        for method, path in (("GET", "/onboarding/v1"), ("POST", "/onboarding/v1/actions")):
            status, _, _ = await raw_call(harness.app, method, path)
            assert status == 401  # before reading a body or creating an account
        assert harness.children == []
        a = (await client.get("/onboarding/v1", headers=harness.headers("a"))).json()
        b = (await client.get("/onboarding/v1", headers=harness.headers("b"))).json()
        assert a["data_epoch"] != b["data_epoch"]
        request = action(a).model_dump()
        result = await client.post("/onboarding/v1/actions", headers=harness.headers("a"), json=request)
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
        assert "Authorization" in result.headers["vary"]
        cross = await client.post("/onboarding/v1/actions", headers=harness.headers("b"), json=request)
        assert cross.status_code == 409 and cross.json()["error"]["code"] == "epoch_mismatch"
        assert (await client.get("/onboarding/v1", headers=harness.headers("b"))).json() == b
        # The same request ID belongs to this other account independently.
        own = await client.post("/onboarding/v1/actions", headers=harness.headers("b"), json=action(b, "denied").model_dump())
        assert own.status_code == 200
        assert own.json()["steps"][0]["decision"] == "denied"
        assert result.json()["steps"][0]["decision"] == "skip"
        assert (await client.get("/onboarding/v1/", headers=harness.headers())).status_code == 404
        assert (await client.put("/onboarding/v1/actions", headers=harness.headers(), json=request)).status_code == 404
        assert (await client.get("/onboarding/v1?subject=b", headers=harness.headers())).status_code == 400
