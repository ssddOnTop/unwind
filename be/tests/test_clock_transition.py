"""Clock-transition regressions: isolated stores, injected time, no network or device calls.

Run with --basetemp under evidence/review and PYTHONDONTWRITEBYTECODE=1.
The legacy tests alter only their own closed temporary SQLite database.
"""
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from uuid import uuid4

import pytest

from companion.bootstrap import UnavailableSynthesis
from sample_data import build_service
from companion.contracts import RULES, validate
from sample_data import BATCH


NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


@asynccontextmanager
async def opened(path, clock):
    service = build_service(
        path,
        replacements={"synthesis": UnavailableSynthesis(path / "checkpoints.sqlite")},
        clock=lambda: clock[0],
    )
    try:
        await service.start()
        yield service
    finally:
        await service.close()


async def mutate(service, route, **fields):
    state = await service.state()
    result = await service.mutate(route, {
        "schema_version": "1", "request_id": str(uuid4()),
        "data_epoch": state["data_epoch"], **fields,
    })
    validate("state", result["state"] if route == "/observations" else result)
    return result


async def seed(service):
    return await mutate(service, "/demo", action="seed",
                        fixture_set="representative-v1", consent_demo=True)


async def sources(service, enabled):
    preferences = deepcopy((await service.state())["preferences"])
    preferences["enabled_source_ids"] = enabled
    return await mutate(service, "/preferences", preferences=preferences)


async def live_record(service):
    observation = deepcopy(BATCH["observations"][3])
    observation.update(id="clock-transition-live-day", source_id="healthkit",
                       mode="live", local_date="2026-11-13")
    observation["provenance"].update(adapter="healthkit_daily_steps",
                                    method="daily_cumulative_sum")
    await mutate(service, "/observations", observations=[observation], source_updates=[])
    return observation


def stored(path):
    with sqlite3.connect(path / "state.sqlite") as db:
        return json.loads(db.execute("SELECT body FROM snapshot WHERE id=1").fetchone()[0])


def force_legacy_fixture_flag(path):
    """Recreate the old persisted flag without touching a running database."""
    with sqlite3.connect(path / "state.sqlite") as db:
        document = json.loads(db.execute("SELECT body FROM snapshot WHERE id=1").fetchone()[0])
        document["meta"]["fixture_session"] = True
        db.execute("UPDATE snapshot SET body=? WHERE id=1", (json.dumps(document),))


def assert_unknown_steps(state):
    card = next(c for c in state["ledger"] if c["id"] == "recorded_steps")
    assert card["recorded_steps"] is None  # absence must not become measured zero
    assert card["days_with_records"] == 0
    assert card["status"] == "insufficient_evidence"
    assert card["coverage"] == "unknown"
    assert card["suggestion"] is None
    assert card["inferred"] is None
    assert card["observed"] == RULES["recorded_steps"]["empty_text"]
    assert card["evidence"] == []
    assert state["notification_intents"] == []


async def test_pure_demo_remains_frozen_through_housekeeping_and_restart(tmp_path):
    clock = [NOW]
    async with opened(tmp_path, clock) as service:
        before = await seed(service)
        clock[0] += timedelta(days=100)
        await service.housekeep()
        assert await service.state() == before
        assert service.document["meta"]["fixture_session"] is True
        assert service.now().isoformat() == RULES["demo"]["now_utc"].replace("Z", "+00:00")
    async with opened(tmp_path, clock) as service:
        assert await service.state() == before
        assert stored(tmp_path)["meta"]["fixture_session"] is True


async def test_empty_healthkit_consent_switches_and_persists_real_clock(tmp_path):
    clock = [NOW]
    async with opened(tmp_path, clock) as service:
        await seed(service)
        clock[0] += timedelta(days=10)
        state = await sources(service, ["demo", "healthkit"])
        assert state["evaluation_date"] == "2026-11-24"
        assert not any(o["mode"] == "live" for o in state["observations"])
        assert next(s for s in state["sources"] if s["id"] == "healthkit")["consent"] == "granted"
        assert_unknown_steps(state)
        assert stored(tmp_path)["meta"]["fixture_session"] is False
    clock[0] += timedelta(days=1)
    async with opened(tmp_path, clock) as service:
        assert (await service.state())["evaluation_date"] == "2026-11-25"
        assert service.document["meta"]["fixture_session"] is False
        assert_unknown_steps(await service.state())


async def test_live_ingestion_then_100_days_prunes_evidence_feedback_and_receipts(tmp_path):
    clock = [NOW]
    async with opened(tmp_path, clock) as service:
        await seed(service)
        await sources(service, ["demo", "healthkit"])
        observation = await live_record(service)
        await mutate(service, "/feedback", card_id="recorded_steps", kind="correctness",
                     value="correct", scope="event",
                     event_ref={"observation_id": observation["id"], "revision": 1},
                     attribution=None, note="")
        clock[0] += timedelta(days=100)
        await service.housekeep()
        state = await service.state()
        assert state["evaluation_date"] == "2027-02-22"
        assert state["observations"] == [] and state["feedback"] == []
        assert_unknown_steps(state)
        assert stored(tmp_path)["meta"]["fixture_session"] is False
        with sqlite3.connect(tmp_path / "state.sqlite") as db:
            assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
        assert observation["id"].encode() not in (tmp_path / "state.sqlite").read_bytes()


async def test_restart_prunes_without_a_prior_housekeeping_pass(tmp_path):
    clock = [NOW]
    async with opened(tmp_path, clock) as service:
        await seed(service)
        await sources(service, ["demo", "healthkit"])
        await live_record(service)
    clock[0] += timedelta(days=100)
    async with opened(tmp_path, clock) as service:
        state = await service.state()
        assert state["evaluation_date"] == "2027-02-22"
        assert state["observations"] == []
        assert_unknown_steps(state)
        assert stored(tmp_path)["meta"]["fixture_session"] is False


@pytest.mark.parametrize("revoked", [False, True], ids=["granted", "revoked"])
async def test_legacy_live_database_uses_real_clock_before_prune_and_persists_migration(tmp_path, revoked):
    clock = [NOW]
    async with opened(tmp_path, clock) as service:
        await seed(service)
        await sources(service, ["demo", "healthkit"])
        await live_record(service)
        if revoked:
            await sources(service, ["demo"])
    force_legacy_fixture_flag(tmp_path)
    assert stored(tmp_path)["meta"]["fixture_session"] is True
    clock[0] += timedelta(days=100)
    async with opened(tmp_path, clock) as service:
        state = await service.state()
        assert state["observations"] == []  # proves wall clock was selected BEFORE startup prune
        assert state["evaluation_date"] == "2027-02-22"
        assert stored(tmp_path)["meta"]["fixture_session"] is False
        assert_unknown_steps(state)
    clock[0] += timedelta(days=1)
    async with opened(tmp_path, clock) as service:
        assert (await service.state())["evaluation_date"] == "2027-02-23"
        assert stored(tmp_path)["meta"]["fixture_session"] is False


async def test_revocation_and_pruning_cannot_reactivate_fixture_time(tmp_path):
    clock = [NOW]
    async with opened(tmp_path, clock) as service:
        await seed(service)
        await sources(service, ["demo", "healthkit"])
        await live_record(service)
        await sources(service, ["demo"])
        clock[0] += timedelta(days=100)
        await service.housekeep()
        state = await service.state()
        assert state["evaluation_date"] == "2027-02-22"
        assert state["observations"] == []
        assert next(s for s in state["sources"] if s["id"] == "healthkit")["consent"] == "revoked"
        assert stored(tmp_path)["meta"]["fixture_session"] is False
        assert_unknown_steps(state)
        clock[0] += timedelta(days=1)
        await service.housekeep()
        assert (await service.state())["evaluation_date"] == "2027-02-23"
    async with opened(tmp_path, clock) as service:
        assert (await service.state())["evaluation_date"] == "2027-02-23"
        assert stored(tmp_path)["meta"]["fixture_session"] is False


async def test_legacy_empty_revoked_consent_history_also_ends_fixture_session(tmp_path):
    clock = [NOW]
    async with opened(tmp_path, clock) as service:
        await seed(service)
        await sources(service, ["demo", "healthkit"])
        await sources(service, ["demo"])
    force_legacy_fixture_flag(tmp_path)
    clock[0] += timedelta(days=100)
    async with opened(tmp_path, clock) as service:
        state = await service.state()
        assert state["evaluation_date"] == "2027-02-22"
        assert state["observations"] == []
        assert stored(tmp_path)["meta"]["fixture_session"] is False
        assert_unknown_steps(state)
