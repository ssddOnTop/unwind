"""Focused offline smoke: no providers, models, live databases or device operations."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from companion.bootstrap import build_service, UnavailableSynthesis
from companion.contracts import DomainError
from companion.domain import iso
from companion.plaid_contracts import validate_profile
from companion.experience_contracts import validate_experience
from sample_data import EXAMPLE

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


def service_at(path, clock=lambda: NOW):
    service = build_service(path, experience_profile=True, clock=clock,
                            replacements={"synthesis": UnavailableSynthesis()})
    service.state_validator = lambda state: validate_profile("state", state)
    return service


def connected_document(service):
    doc = deepcopy(service.document)
    state = doc["state"]
    rows = deepcopy([o for o in EXAMPLE["observations"] if o["kind"] == "purchase"])
    for index, row in enumerate(rows):
        row.update(id=f"sandbox-record-{index}", source_id="plaid")
        row["provenance"].update(adapter="plaid_sandbox", method="transactions_sync", record_id=f"sandbox-{index}")
    state["observations"] = rows
    state["preferences"]["enabled_source_ids"] = ["plaid"]
    source = next(s for s in state["sources"] if s["id"] == "plaid")
    source.update(enabled=True, consent="granted", capability="available", status="ready",
                  authorization_requested=True, last_success_at=iso(NOW))
    doc["meta"]["plaid_connected"] = True
    return doc


@pytest.mark.asyncio
async def test_empty_runtime_rejects_seeding_scripted_and_fixture_ingestion(tmp_path):
    service = service_at(tmp_path)
    await service.start()
    try:
        state = await service.state()
        assert state["observations"] == []
        assert state["preferences"]["enabled_source_ids"] == []
        assert (await service.experience_coordinator.get())["inference_history"] == []
        for route, body in [("/demo", {}), ("/observations", {"observations": EXAMPLE["observations"]}),
                            ("/preferences", {"preferences": {"enabled_source_ids": ["demo"]}})]:
            with pytest.raises(DomainError):
                await service.mutate(route, body)
        with pytest.raises(DomainError):
            await service.process({"synthesis_mode": "scripted"})
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_inference_timestamp_stable_after_refresh_restart_and_disabled_accounts(tmp_path):
    clock = [NOW]
    service = service_at(tmp_path, lambda: clock[0])
    await service.start()
    doc = connected_document(service)
    await service.evaluate(doc)
    await service.commit(doc)
    first = await service.experience_coordinator.get()
    validate_experience("experience", first)
    assert len(first["inference_history"]) == 1
    entry = first["inference_history"][0]
    assert entry["inferred_at"] == iso(NOW)
    assert entry["status"] == "unconfirmed"
    assert entry["method"] == "Deterministic rule"
    assert len(entry["evidence"]) == 3
    assert all(e["source"] == "Plaid Sandbox" for e in entry["evidence"])
    assert first["home"]["sample_label"].startswith("Plaid Sandbox")
    clock[0] += timedelta(minutes=20)
    assert (await service.experience_coordinator.get())["inference_history"] == [entry]
    await service.close()
    service = service_at(tmp_path, lambda: clock[0])
    await service.start()
    try:
        assert (await service.experience_coordinator.get())["inference_history"] == [entry]
        doc = deepcopy(service.document)
        doc["meta"]["banking_excluded_ids"] = [o["id"] for o in doc["state"]["observations"]]
        await service.evaluate(doc)
        await service.commit(doc)
        disabled = await service.experience_coordinator.get()
        assert disabled["inference_history"] == [entry]
        assert disabled["visit"]["inferred_habits"] == []
        assert disabled["home"]["status"] != "pattern"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_legacy_inference_has_unknown_timestamp_and_demo_history_is_excluded(tmp_path):
    service = service_at(tmp_path)
    await service.start()
    doc = connected_document(service)
    for key in list(doc["meta"]):
        if key.startswith("inference_history"):
            del doc["meta"][key]
    await service.registry.dispatch("persistence", "commit", {"document": doc})
    await service.close()
    service = service_at(tmp_path)
    await service.start()
    try:
        entry = (await service.experience_coordinator.get())["inference_history"][0]
        assert entry["inferred_at"] is None
        doc = deepcopy(service.document)
        doc["state"]["observations"] = deepcopy(EXAMPLE["observations"])
        await service.evaluate(doc)
        await service.commit(doc)
        value = await service.experience_coordinator.get()
        assert value["inference_history"] == []
        assert value["visit"]["inferred_habits"] == []
        assert value["home"]["sample_label"] is None
        assert service.document["state"]["observations"]  # Stored history was not deleted.
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_answer_appends_correction_once_and_erasure_clears_history(tmp_path):
    service = service_at(tmp_path)
    await service.start()
    try:
        doc = connected_document(service)
        await service.evaluate(doc)
        await service.commit(doc)
        value = await service.experience_coordinator.get()
        request = dict(schema_version="1", request_id="correction", data_epoch=value["data_epoch"],
                       expected_revision=value["revision"], clarification_key=value["clarification"]["key"], answer="no")
        corrected = await service.experience_coordinator.answer(request)
        assert len(corrected["inference_history"]) == 2
        assert corrected["inference_history"][-1]["status"] == "corrected"
        assert await service.experience_coordinator.answer(request) == corrected
        assert (await service.experience_coordinator.get())["inference_history"] == corrected["inference_history"]
        await service.mutate("/data", dict(schema_version="1", request_id="erase", data_epoch=value["data_epoch"]))
        assert (await service.experience_coordinator.get())["inference_history"] == []
    finally:
        await service.close()


def test_shipped_runtime_has_no_example_data_dependencies():
    root = Path(__file__).resolve().parents[2]
    for path in (root / "be/companion").rglob("*.py"):
        text = path.read_text()
        assert "state.example.json" not in text
        assert "observation-batch.example.json" not in text
        assert "FakeMessagesListChatModel" not in text
    assert not list((root / "contracts").glob("*.example.json"))
