"""Daily coordinator conformance through public service/state/process boundaries only."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from companion import http
from companion.bootstrap import build_service
from companion.contracts import BATCH, DomainError, EXAMPLE, validate
from companion.daily import DailyCoordinator
from companion.domain import iso, synthesis

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
DAY = timedelta(hours=24)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in ("COMPANION_DAILY_SYNTHESIS_MODE", "COMPANION_LLM_PROVIDER", "COMPANION_LLM_MODEL",
                 "OPENROUTER_API_KEY", "REQUESTY_API_KEY", "LANGSMITH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    original = socket.socket.connect

    def deny_network(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("Daily tests must never make network/provider requests")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", deny_network)


class SynthesisDouble:
    id, version, status = "synthesis", "1", "available"

    def __init__(self):
        self.calls = []
        self.outcome, self.reason = "completed", None
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()

    async def handle(self, operation, payload):
        if operation == "clear":
            return {"cleared": True}
        self.calls.append(deepcopy(payload))
        self.entered.set()
        await self.release.wait()
        return synthesis(payload["mode"], self.outcome, self.reason)


async def populate(service):
    state = await service.state()
    common = {"schema_version": "1", "data_epoch": state["data_epoch"]}
    await service.mutate("/preferences", {**common, "request_id": "consent", "preferences": state["preferences"]})
    await service.mutate("/observations", {**common, "request_id": "batch",
                                         "observations": BATCH["observations"], "source_updates": []})


@pytest.fixture
async def backend(tmp_path):
    clock, extension = [NOW], SynthesisDouble()
    service = build_service(tmp_path, replacements={"synthesis": extension}, clock=lambda: clock[0])
    await service.start()
    await populate(service)
    try:
        yield service, clock, extension
    finally:
        extension.release.set()
        await service.close()


def public_double():
    state = deepcopy(EXAMPLE)
    state["process"].update(status="idle", run_id=None, started_at=None, completed_at=None)
    return AsyncMock(state=AsyncMock(return_value=state), process=AsyncMock(return_value=state)), state


async def test_default_disabled_and_explicit_override(backend, monkeypatch):
    service, clock, extension = backend
    before = await service.state()
    assert await DailyCoordinator(service).tick() is None
    monkeypatch.setenv("COMPANION_DAILY_SYNTHESIS_MODE", "scripted")
    assert await DailyCoordinator(service, mode="disabled").tick() is None
    assert await service.state() == before
    assert not extension.calls
    assert await DailyCoordinator(service, clock=lambda: clock[0]).tick() is not None
    assert len(extension.calls) == 1


@pytest.mark.parametrize("mode", ["", "auto", "LIVE"])
def test_invalid_mode_fails_closed(mode, monkeypatch):
    monkeypatch.setenv("COMPANION_DAILY_SYNTHESIS_MODE", mode)
    with pytest.raises(ValueError):
        DailyCoordinator(None)


async def test_first_attempt_then_elapsed_24h_and_new_receipts(backend):
    service, clock, extension = backend
    coordinator = DailyCoordinator(service, mode="scripted", clock=lambda: clock[0])
    results = [await coordinator.tick()]
    assert results[0]["process"]["started_at"] == iso(NOW)
    assert await coordinator.tick() is None
    # Same service, fresh coordinator: no in-memory scheduling state is needed.
    coordinator = DailyCoordinator(service, mode="scripted", clock=lambda: clock[0])
    clock[0] += DAY - timedelta(microseconds=1)
    assert await coordinator.tick() is None
    clock[0] += timedelta(microseconds=1)
    results.append(await coordinator.tick())
    clock[0] += DAY
    results.append(await coordinator.tick())
    assert len({r["process"]["run_id"] for r in results}) == 3
    assert len(extension.calls) == 3
    for result in results:
        validate("state", result)


@pytest.mark.parametrize("status,reason", [("failed", "timeout"), ("skipped", "no_credentials")])
async def test_failed_and_skipped_synthesis_retry_only_next_24h(backend, status, reason):
    service, clock, extension = backend
    extension.outcome, extension.reason = status, reason
    state = await service.state()
    state["preferences"]["cloud_consent"] = True
    await service.mutate("/preferences", {"schema_version": "1", "data_epoch": state["data_epoch"],
                                         "request_id": "cloud", "preferences": state["preferences"]})
    first = await DailyCoordinator(service, mode="live", clock=lambda: clock[0]).tick()
    assert first["process"]["started_at"] == iso(NOW)
    assert first["process"]["synthesis"]["status"] == status
    assert first["process"]["synthesis"]["reason"] == reason
    clock[0] += DAY - timedelta(seconds=1)
    assert await DailyCoordinator(service, mode="live", clock=lambda: clock[0]).tick() is None
    clock[0] += timedelta(seconds=1)
    second = await DailyCoordinator(service, mode="live", clock=lambda: clock[0]).tick()
    assert second["process"]["run_id"] != first["process"]["run_id"]
    assert len(extension.calls) == 2


async def test_sqlite_restart_preserves_attempt_and_next_run(tmp_path):
    clock, extension = [NOW], SynthesisDouble()
    def build():
        return build_service(tmp_path, replacements={"synthesis": extension}, clock=lambda: clock[0])
    service = build()
    await service.start()
    try:
        await populate(service)
        first = await DailyCoordinator(service, mode="scripted", clock=lambda: clock[0]).tick()
    finally:
        await service.close()
    service = build()
    await service.start()
    try:
        coordinator = DailyCoordinator(service, mode="scripted", clock=lambda: clock[0])
        assert await coordinator.tick() is None
        clock[0] += DAY
        await service.housekeep()
        second = await coordinator.tick()
        assert second["process"]["run_id"] != first["process"]["run_id"]
        assert len(extension.calls) == 2
    finally:
        await service.close()


async def test_running_and_concurrent_ticks_do_not_duplicate(backend):
    service, clock, extension = backend
    coordinator = DailyCoordinator(service, mode="scripted", clock=lambda: clock[0])
    extension.release.clear()
    task = asyncio.create_task(coordinator.tick())
    try:
        await asyncio.wait_for(extension.entered.wait(), timeout=2)
        clock[0] += 2 * DAY  # Even an overdue running job must be left alone.
        assert await coordinator.tick() is None
        assert await DailyCoordinator(service, mode="scripted", clock=lambda: clock[0]).tick() is None
        assert len(extension.calls) == 1
    finally:
        extension.release.set()
        await task


@pytest.mark.parametrize("change", ["empty", "revoked", "disabled", "suppressed", "no_suggestion", "missing_evidence"])
async def test_no_eligible_consented_evidence_is_read_only(change):
    service, state = public_double()
    if change == "empty":
        state["observations"] = []
    elif change in {"revoked", "disabled"}:
        for source in state["sources"]:
            source.update(consent="revoked" if change == "revoked" else "granted", enabled=change != "disabled")
    elif change == "suppressed":
        state["preferences"]["suppressed_scenario_ids"] = [c["scenario_id"] for c in state["ledger"]]
    elif change == "no_suggestion":
        for card in state["ledger"]:
            card["suggestion"] = None
    else:
        for card in state["ledger"]:
            card["evidence"] = []
    before = deepcopy(state)
    assert await DailyCoordinator(service, mode="scripted", clock=lambda: NOW).tick() is None
    service.process.assert_not_awaited()
    assert state == before


async def test_preferences_suppression_and_revocation_real_service(backend):
    service, clock, extension = backend
    state = await service.state()
    common = {"schema_version": "1", "data_epoch": state["data_epoch"]}
    preferences = state["preferences"]
    preferences.update(food_suggestions=False, activity_suggestions=False)
    await service.mutate("/preferences", {**common, "request_id": "suppress", "preferences": preferences})
    assert await DailyCoordinator(service, mode="scripted", clock=lambda: clock[0]).tick() is None
    preferences.update(food_suggestions=True, activity_suggestions=True, enabled_source_ids=[])
    await service.mutate("/preferences", {**common, "request_id": "revoke", "preferences": preferences})
    assert await DailyCoordinator(service, mode="scripted", clock=lambda: clock[0]).tick() is None
    assert not extension.calls


@pytest.mark.parametrize("started", ["not-a-time", "", "2026-11-13", "2026-11-13T12:00:00", 42,
                                      "2026-11-15T12:00:00Z", None])
async def test_invalid_missing_or_future_recorded_timestamp_skips(started):
    service, state = public_double()
    state["process"].update(status="failed", run_id="interrupted", started_at=started)
    assert await DailyCoordinator(service, mode="scripted", clock=lambda: NOW).tick() is None
    service.process.assert_not_awaited()


@pytest.mark.parametrize("clock", [lambda: NOW.replace(tzinfo=None), lambda: None, lambda: "invalid"])
async def test_invalid_injected_clock_skips(clock):
    service, _ = public_double()
    assert await DailyCoordinator(service, mode="scripted", clock=clock).tick() is None
    service.process.assert_not_awaited()


async def test_clock_rollback_and_manual_disabled_attempt_count(backend):
    service, clock, extension = backend
    state = await service.state()
    await service.process({"schema_version": "1", "data_epoch": state["data_epoch"],
                           "request_id": "manual-disabled", "synthesis_mode": "disabled"})
    coordinator = DailyCoordinator(service, mode="scripted", clock=lambda: clock[0])
    clock[0] -= DAY
    assert await coordinator.tick() is None
    clock[0] = NOW + DAY - timedelta(seconds=1)
    assert await coordinator.tick() is None
    clock[0] += timedelta(seconds=1)
    assert await coordinator.tick() is not None
    assert len(extension.calls) == 2


@pytest.mark.parametrize("status", ["completed", "failed"])
@pytest.mark.parametrize("started,now,due", [
    ("2026-03-07T12:00:00-05:00", "2026-03-08T12:00:00-04:00", False),
    ("2026-03-07T12:00:00-05:00", "2026-03-08T13:00:00-04:00", True),
    ("2026-10-31T12:00:00-04:00", "2026-11-01T11:00:00-05:00", True),
])
async def test_dst_elapsed_not_calendar_and_failed_process(status, started, now, due):
    service, state = public_double()
    state["process"].update(status=status, run_id="prior", started_at=started)
    clock = lambda: datetime.fromisoformat(now).astimezone(ZoneInfo("America/New_York"))
    result = await DailyCoordinator(service, mode="scripted", clock=clock).tick()
    assert (result is not None) == due
    assert service.process.await_count == int(due)


@pytest.mark.parametrize("mixed", [True, False])
async def test_scripted_skips_any_live_input_even_if_revoked(mixed):
    service, state = public_double()
    for observation in state["observations"][:1 if mixed else len(state["observations"])]:
        observation.update(mode="live", source_id="healthkit")
    assert await DailyCoordinator(service, mode="scripted", clock=lambda: NOW).tick() is None
    service.process.assert_not_awaited()


@pytest.mark.parametrize("mode,configured,cloud,reason", [
    ("scripted", False, False, None),
    ("live", False, False, "no_credentials"),
    ("live", True, False, "cloud_consent_required"),
    ("live", True, True, "live_not_approved"),
])
async def test_real_synthesis_gates_and_scripted_no_network(tmp_path, monkeypatch, mode, configured, cloud, reason):
    if configured:
        monkeypatch.setenv("COMPANION_LLM_PROVIDER", "openrouter")
        monkeypatch.setenv("COMPANION_LLM_MODEL", "openrouter/free")
        monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-never-a-credential")
    service = build_service(tmp_path, clock=lambda: NOW)
    await service.start()
    try:
        await populate(service)
        if cloud:
            state = await service.state()
            common = {"schema_version": "1", "data_epoch": state["data_epoch"]}
            state["preferences"].update(cloud_consent=True, enabled_source_ids=["demo", "healthkit"])
            await service.mutate("/preferences", {**common, "request_id": "cloud", "preferences": state["preferences"]})
            observation = deepcopy(BATCH["observations"][3])
            observation.update(id="healthkit-daily", source_id="healthkit", mode="live")
            observation["provenance"].update(adapter="healthkit_daily_steps", method="daily_cumulative_sum")
            await service.mutate("/observations", {**common, "request_id": "live", "observations": [observation], "source_updates": []})
        coordinator = DailyCoordinator(service, mode=mode, clock=lambda: NOW)
        result = await coordinator.tick()
        assert result["process"]["synthesis"]["reason"] == reason
        if mode == "scripted":
            assert result["process"]["synthesis"]["status"] == "completed"
            assert result["process"]["synthesis"]["drafts"]
        assert await coordinator.tick() is None
    finally:
        await service.close()


@pytest.mark.parametrize("code", ["busy", "epoch_mismatch", "invalid_request", "consent_required", "internal_error"])
async def test_admission_races_skip_but_unexpected_failure_propagates(code):
    service, _ = public_double()
    service.process.side_effect = DomainError(code, 409 if code != "internal_error" else 500)
    coordinator = DailyCoordinator(service, mode="scripted", clock=lambda: NOW)
    if code == "internal_error":
        with pytest.raises(DomainError):
            await coordinator.tick()
    else:
        assert await coordinator.tick() is None


async def test_request_id_stable_until_recorded_attempt_then_changes():
    service, state = public_double()
    coordinator = DailyCoordinator(service, mode="scripted", clock=lambda: NOW)
    await coordinator.tick()
    first = deepcopy(service.process.call_args.args[0])
    await DailyCoordinator(service, mode="scripted", clock=lambda: NOW + DAY).tick()
    assert service.process.call_args.args[0] == first
    state["process"].update(status="failed", run_id="new-attempt", started_at=iso(NOW - DAY))
    await coordinator.tick()
    second = deepcopy(service.process.call_args.args[0])
    assert first["request_id"] != second["request_id"]
    state["data_epoch"] = "new-epoch"
    await coordinator.tick()
    assert service.process.call_args.args[0]["request_id"] != second["request_id"]
    validate("process_request", first)
    validate("process_request", second)


@pytest.fixture
async def persisted_demo(tmp_path):
    service = build_service(tmp_path, replacements={"synthesis": SynthesisDouble()}, clock=lambda: NOW)
    await service.start()
    try:
        state = await service.state()
        seeded = await service.mutate("/demo", {"schema_version": "1", "data_epoch": state["data_epoch"],
            "request_id": "seed", "action": "seed", "fixture_set": "representative-v1", "consent_demo": True})
    finally:
        await service.close()
    return tmp_path, seeded


@pytest.fixture
def housekeeping_sleep(monkeypatch):
    sleeping = asyncio.Event()

    async def sleep_until_cancelled(_):
        sleeping.set()
        await asyncio.Event().wait()

    # Replace HTTP's reference only, never asyncio.sleep globally or coordinator behavior.
    monkeypatch.setattr(http, "asyncio", SimpleNamespace(create_task=asyncio.create_task,
        CancelledError=asyncio.CancelledError, Event=asyncio.Event, sleep=sleep_until_cancelled))
    return sleeping


@pytest.mark.parametrize("mode", [None, "scripted"])
async def test_lifespan_immediate_tick_on_persisted_seed(persisted_demo, housekeeping_sleep, monkeypatch, mode):
    path, seeded = persisted_demo
    if mode:
        monkeypatch.setenv("COMPANION_DAILY_SYNTHESIS_MODE", mode)
    ticked = asyncio.Event()
    original_tick = DailyCoordinator.tick

    async def observe_tick(coordinator):
        result = await original_tick(coordinator)
        ticked.set()
        return result

    monkeypatch.setattr(DailyCoordinator, "tick", observe_tick)
    app = http.create_app(path, clock=lambda: NOW + DAY, housekeeping_interval=86400)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(housekeeping_sleep.wait(), timeout=5)
        assert ticked.is_set(), "Coordinator must tick before the first housekeeping sleep"
        state = await app.state.service.state()
        assert not app.state.housekeeping_failed
        if mode is None:
            assert state["process"] == seeded["process"]
        else:
            assert state["process"]["started_at"] == iso(NOW + DAY)
            assert state["process"]["run_id"] != seeded["process"]["run_id"]
            assert state["process"]["synthesis"]["status"] == "completed"
            assert state["process"]["synthesis"]["drafts"]
    # Reopening the same storage proves normal lifespan shutdown released its writer lock.
    reopened = build_service(path, clock=lambda: NOW + DAY)
    await reopened.start()
    try:
        assert (await reopened.state())["process"] == state["process"]
    finally:
        await reopened.close()


async def test_lifespan_shutdown_cancels_active_daily_work(persisted_demo, housekeeping_sleep, monkeypatch):
    path, _ = persisted_demo
    monkeypatch.setenv("COMPANION_DAILY_SYNTHESIS_MODE", "scripted")
    cancelled, cleared = asyncio.Event(), asyncio.Event()

    class BlockingSynthesis(SynthesisDouble):
        async def handle(self, operation, payload):
            if operation == "clear":
                cleared.set()
            try:
                return await super().handle(operation, payload)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    extension = BlockingSynthesis()
    extension.release.clear()
    app = http.create_app(path, replacements={"synthesis": extension}, clock=lambda: NOW + DAY)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        await asyncio.wait_for(extension.entered.wait(), timeout=2)
        assert (await app.state.service.state())["process"]["status"] == "running"
    finally:
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        resumed_sleep = asyncio.create_task(housekeeping_sleep.wait())
        try:
            done, _ = await asyncio.wait({shutdown, resumed_sleep}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
            clean_shutdown = shutdown in done
        finally:
            # A broken shutdown must not leak the worker or SQLite lock into other tests.
            if not shutdown.done():
                shutdown.cancel()
            resumed_sleep.cancel()
            await asyncio.wait_for(asyncio.gather(shutdown, resumed_sleep, return_exceptions=True), timeout=2)
    assert cancelled.is_set() and cleared.is_set()
    reopened = build_service(path, replacements={"synthesis": SynthesisDouble()}, clock=lambda: NOW + DAY)
    await reopened.start()
    try:
        state = await reopened.state()
        assert state["process"]["status"] != "running"
        assert state["process"]["synthesis"]["status"] == "failed"
        assert not state["process"]["synthesis"]["drafts"]
    finally:
        await reopened.close()
    assert clean_shutdown, "Shutdown cancellation was swallowed: housekeeping resumed sleeping instead of closing"
