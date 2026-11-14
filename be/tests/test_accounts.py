"""Account lifecycle and real-bootstrap isolation; no providers or dotenv imports."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from companion.accounts import AccountError, AccountManager, UserWorkContext
from test_auth import Harness, claims, keys, token


class FakeService:
    def __init__(self, path):
        self.path, self.starts, self.closes = path, 0, 0
        self.start_gate = None
        self.close_failure = False

    async def start(self):
        self.starts += 1
        if self.start_gate:
            await self.start_gate.wait()
        (self.path / "state.sqlite").write_bytes(b"scratch fake state")
        (self.path / "checkpoints.sqlite").write_bytes(b"scratch fake checkpoint")
        (self.path / "vault.json").write_text('{}')

    async def close(self):
        self.closes += 1
        if self.close_failure:
            raise RuntimeError("scratch close failure")


@pytest.fixture
async def principals(keys):
    h = Harness(keys)
    result = [await h.verify(sub="../outside/user-a"), await h.verify(sub="user-b"),
              await h.verify(sub="../outside/user-a", sid="new-session")]
    # A second independently configured issuer uses the same scratch signing key.
    from companion.auth import AuthConfig
    other = Harness(keys, AuthConfig("https://other.example.com"))
    result.append(await other.verify(token(keys[0], claims(iss="https://other.example.com", sub="../outside/user-a"))))
    yield result
    await h.verifier.aclose()
    await other.verifier.aclose()


async def test_concurrent_single_start_identity_sessions_issuer_and_paths(tmp_path, principals):
    created = []
    async def factory(path):
        await asyncio.sleep(0)
        service = FakeService(path)
        created.append(service)
        return service
    root = tmp_path / "auth"
    manager = AccountManager(root, factory)
    assert not root.exists()  # No storage opened before authenticated admission.
    with pytest.raises(TypeError):
        await manager.service_for("user-a")
    assert not root.exists()
    try:
        handles = await asyncio.gather(*(manager.service_for(principals[0]) for _ in range(20)))
        a = handles[0]
        assert all(h is a for h in handles)
        assert (await manager.service_for(principals[2])) is a
        b = await manager.service_for(principals[1])
        other_issuer = await manager.service_for(principals[3])
        assert len({h.account_id for h in (a, b, other_issuer)}) == 3
        assert all(service.starts == 1 for service in created)
        assert len(await manager.handles()) == 3
        assert all(h.data_dir.parent == root / "users" and h.data_dir.name == h.account_id for h in (a, b, other_issuer))
        assert "user-a" not in str(a.data_dir)
        for base, dirs, files in os.walk(root):
            assert Path(base).stat().st_mode & 0o777 == 0o700
            for name in files:
                assert (Path(base) / name).stat().st_mode & 0o777 == 0o600
        db = sqlite3.connect(root / "identities.sqlite")
        assert db.execute("SELECT count(*) FROM identities").fetchone() == (3,)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO identities VALUES(?,?,?)", (principals[0].issuer, principals[0].subject, str(uuid4())))
        db.close()
        assert b"session_scratch" not in (root / "identities.sqlite").read_bytes()
        context = UserWorkContext(a.account_id, "epoch", 1)
        assert context.account_id == a.account_id
    finally:
        await manager.aclose()
        await manager.aclose()
    assert all(s.closes == 1 for s in created)
    # Stable mapping after restart, not session-specific and not an exposed selector.
    reopened = AccountManager(root, FakeService)
    try:
        assert (await reopened.service_for(principals[0])).account_id == a.account_id
    finally:
        await reopened.aclose()


async def test_failed_authentication_never_opens_storage(tmp_path, keys):
    from companion.auth import AuthError
    h = Harness(keys)
    root = tmp_path / "auth"
    manager = AccountManager(root, FakeService)
    async def protected(headers):
        principal = await h.verifier.authenticate_request(headers)
        return await manager.service_for(principal)
    try:
        for jwt in [None, token(keys[1]), token(keys[0], claims(exp=1)), token(keys[0], claims(sts="pending"))]:
            headers = [] if jwt is None else [(b"authorization", ("Bearer " + jwt).encode())]
            with pytest.raises(AuthError):
                await protected(headers)
            assert not root.exists()
        await protected([(b"authorization", ("Bearer " + token(keys[0])).encode())])
        assert root.exists()
    finally:
        await manager.aclose()
        await h.verifier.aclose()


async def test_capacity_refuses_without_eviction_or_identity_growth(tmp_path, principals):
    manager = AccountManager(tmp_path / "auth", FakeService, max_accounts=1)
    try:
        a = await manager.service_for(principals[0])
        for _ in range(3):
            with pytest.raises(AccountError, match="account_capacity"):
                await manager.service_for(principals[1])
        assert await manager.service_for(principals[0]) is a
        assert a.service.closes == 0
        assert manager._db.execute("SELECT count(*) FROM identities").fetchone() == (1,)
        await manager.close_account(principals[0])
        assert a.service.closes == 1
        await manager.service_for(principals[1])
    finally:
        await manager.aclose()


async def test_cancelled_caller_initialization_is_drained(tmp_path, principals):
    gate = asyncio.Event()
    entered = asyncio.Event()
    def factory(path):
        service = FakeService(path)
        service.start_gate = gate
        entered.set()
        return service
    manager = AccountManager(tmp_path / "auth", factory)
    task = asyncio.create_task(manager.service_for(principals[0]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    closing = asyncio.create_task(manager.aclose())
    await asyncio.sleep(0)
    assert not closing.done()
    gate.set()
    await closing
    assert not await manager.handles()
    with pytest.raises(AccountError):
        await manager.service_for(principals[1])


async def test_failed_start_closed_and_retryable(tmp_path, principals):
    created = []
    class Broken(FakeService):
        async def start(self):
            raise RuntimeError("scratch startup error")
    def factory(path):
        service = Broken(path) if not created else FakeService(path)
        created.append(service)
        return service
    manager = AccountManager(tmp_path / "auth", factory)
    try:
        with pytest.raises(AccountError) as e:
            await manager.service_for(principals[0])
        assert str(e.value) == "accounts_unavailable"
        assert created[0].closes == 1
        assert not await manager.handles()
        await manager.service_for(principals[0])
        assert len(created) == 2
    finally:
        await manager.aclose()


async def test_failed_close_retains_handle_and_registry_lock(tmp_path, principals):
    manager = AccountManager(tmp_path / "auth", FakeService)
    a = await manager.service_for(principals[0])
    a.service.close_failure = True
    with pytest.raises(AccountError):
        await manager.aclose()
    assert await manager.handles() == (a,)
    second = AccountManager(tmp_path / "auth", FakeService)
    try:
        with pytest.raises(AccountError):
            await second.service_for(principals[0])
    finally:
        await second.aclose()
    a.service.close_failure = False
    await manager.aclose()


async def test_no_legacy_adoption_no_symlink_paths(tmp_path, principals):
    anonymous = tmp_path / "anonymous"
    anonymous.mkdir()
    anonymous.chmod(0o750)
    legacy = anonymous / "state.sqlite"
    legacy.write_bytes(b"anonymous private legacy")
    bad = AccountManager(anonymous, FakeService)
    try:
        with pytest.raises(AccountError):
            await bad.service_for(principals[0])
        assert legacy.read_bytes() == b"anonymous private legacy"
        assert anonymous.stat().st_mode & 0o777 == 0o750
        assert {p.name for p in anonymous.iterdir()} == {"state.sqlite"}
    finally:
        await bad.aclose()
    alias = tmp_path / "alias"
    alias.symlink_to(anonymous, target_is_directory=True)
    manager = AccountManager(alias, FakeService)
    try:
        with pytest.raises(AccountError):
            await manager.service_for(principals[0])
    finally:
        await manager.aclose()
    good = AccountManager(tmp_path / "auth", FakeService)
    try:
        h = await good.service_for(principals[0])
        assert (h.data_dir / "state.sqlite").read_bytes() != legacy.read_bytes()
        assert legacy.read_bytes() == b"anonymous private legacy"
    finally:
        await good.aclose()


async def test_reused_service_factory_is_rejected_without_closing_a(tmp_path, principals):
    shared = None
    def factory(path):
        nonlocal shared
        shared = shared or FakeService(path)
        return shared
    manager = AccountManager(tmp_path / "auth", factory)
    try:
        a = await manager.service_for(principals[0])
        with pytest.raises(AccountError):
            await manager.service_for(principals[1])
        assert a.service.closes == 0
        assert len(await manager.handles()) == 1
    finally:
        await manager.aclose()


async def test_cancelled_explicit_close_is_drained(tmp_path, principals):
    entered, release = asyncio.Event(), asyncio.Event()
    class BlockingClose(FakeService):
        async def close(self):
            entered.set()
            await release.wait()
            await super().close()
    manager = AccountManager(tmp_path / "auth", BlockingClose)
    a = await manager.service_for(principals[0])
    closing = asyncio.create_task(manager.close_account(principals[0]))
    await entered.wait()
    closing.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert a.service.closes == 1
    assert not await manager.handles()
    await manager.aclose()


async def test_corrupt_internal_id_and_account_symlink_never_escape(tmp_path, principals):
    manager = AccountManager(tmp_path / "auth", FakeService)
    try:
        a = await manager.service_for(principals[0])
        await manager.close_account(principals[0])
        outside = tmp_path / "outside"
        outside.mkdir()
        for p in a.data_dir.iterdir():
            p.unlink()
        a.data_dir.rmdir()
        a.data_dir.symlink_to(outside, target_is_directory=True)
        with pytest.raises(AccountError):
            await manager.service_for(principals[0])
        assert not list(outside.iterdir())
        manager._db.execute("UPDATE identities SET account_id=? WHERE issuer=? AND subject=?",
                            ("../../escape", principals[0].issuer, principals[0].subject))
        with pytest.raises(AccountError):
            await manager.service_for(principals[0])
        assert not (tmp_path / "escape").exists()
    finally:
        await manager.aclose()


async def test_real_bootstrap_identical_epoch_request_id_delete_and_receipts(tmp_path, principals):
    from companion.bootstrap import UnavailableSynthesis, build_service
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    def factory(directory):
        # No synthesis import, model API, Plaid API, or environment discovery.
        return build_service(directory, replacements={"synthesis": UnavailableSynthesis(directory / "checkpoints.sqlite")},
                             clock=lambda: now)
    manager = AccountManager(tmp_path / "auth", factory)
    try:
        a, b = await asyncio.gather(*(manager.service_for(p) for p in principals[:2]))
        assert a.service.registry is not b.service.registry
        assert a.service.scheduler is not b.service.scheduler
        assert a.service.lock is not b.service.lock
        assert a.service.document is not b.service.document
        epoch, request_id = str(uuid4()), str(uuid4())
        for h in (a, b):
            document = deepcopy(h.service.document)
            document["state"]["data_epoch"] = epoch
            await h.service.commit(document, bump=False)
        bodies = []
        for h, tz in [(a, "America/New_York"), (b, "America/Los_Angeles")]:
            preferences = deepcopy((await h.service.state())["preferences"])
            preferences["timezone"] = tz
            body = dict(schema_version="1", data_epoch=epoch, request_id=request_id, preferences=preferences)
            bodies.append(body)
            result = await h.service.mutate("/preferences", body)
            assert result["preferences"]["timezone"] == tz
        assert await a.service.mutate("/preferences", bodies[0]) != await b.service.mutate("/preferences", bodies[1])
        before = {p.name: p.read_bytes() for p in b.data_dir.iterdir() if p.is_file()}
        before_state = await b.service.state()
        deleted = await a.service.mutate("/data", dict(schema_version="1", data_epoch=epoch, request_id=str(uuid4())))
        assert deleted["data_epoch"] != epoch
        assert (await b.service.state()) == before_state
        assert before == {p.name: p.read_bytes() for p in b.data_dir.iterdir() if p.is_file()}
        # B's original same-ID receipt is still usable after A's deletion.
        assert await b.service.mutate("/preferences", bodies[1]) == before_state
        for h in (a, b):
            with sqlite3.connect(h.data_dir / "state.sqlite") as db:
                assert db.execute("SELECT count(*) FROM snapshot").fetchone() == (1,)
    finally:
        await manager.aclose()


async def test_real_bootstrap_late_a_completion_after_delete_never_commits_into_b(tmp_path, principals):
    from companion.bootstrap import UnavailableSynthesis, build_service
    from companion.domain import synthesis
    began, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class LateSynthesis(UnavailableSynthesis):
        async def handle(self, operation, payload):
            if operation != "synthesize":
                return await super().handle(operation, payload)
            began.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Simulate a provider completion arriving despite cancellation.
                cancelled.set()
                await release.wait()
            return synthesis(payload["mode"], "completed")
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    manager = AccountManager(tmp_path / "auth", lambda d: build_service(d,
        replacements={"synthesis": LateSynthesis(d / "checkpoints.sqlite")}, clock=lambda: now))
    try:
        a, b = await asyncio.gather(*(manager.service_for(p) for p in principals[:2]))
        epoch = (await a.service.state())["data_epoch"]
        before_b = await b.service.state()
        before_bytes = {p.name: p.read_bytes() for p in b.data_dir.iterdir() if p.is_file()}
        process = asyncio.create_task(a.service.mutate("/process", dict(schema_version="1", data_epoch=epoch,
            request_id=str(uuid4()), synthesis_mode="disabled")))
        await asyncio.wait_for(began.wait(), timeout=2)
        deletion = asyncio.create_task(a.service.mutate("/data", dict(schema_version="1", data_epoch=epoch,
            request_id=str(uuid4()))))
        await asyncio.wait_for(cancelled.wait(), timeout=2)
        release.set()
        result = await asyncio.wait_for(deletion, timeout=2)
        late = await asyncio.wait_for(process, timeout=2)
        assert result["data_epoch"] != epoch
        assert late == result == await a.service.state()
        assert late["process"]["status"] == "idle"
        assert before_b == await b.service.state()
        assert before_bytes == {p.name: p.read_bytes() for p in b.data_dir.iterdir() if p.is_file()}
    finally:
        release.set()
        await manager.aclose()
