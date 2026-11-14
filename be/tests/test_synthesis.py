"""No-key conformance tests: real SDK graph, bounded models and isolated checkpoints."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

import pytest
from jsonschema import Draft202012Validator
from langchain_core.messages import AIMessage

from companion.extensions import synthesis as module

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for key in ("COMPANION_LLM_PROVIDER", "COMPANION_LLM_MODEL", "OPENROUTER_API_KEY", "REQUESTY_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    def deny(*args, **kwargs):
        raise AssertionError("Unexpected socket connection")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")  # extension must override even opt-in ambient tracing
    monkeypatch.setenv("LANGSMITH_API_KEY", "synthetic-never-a-credential")


@pytest.fixture
def checkpoint():
    # Keep even transient fixtures under the workspace, not system temp.
    with tempfile.TemporaryDirectory(prefix=".synthesis-", dir=Path(__file__).parent) as directory:
        yield Path(directory) / "checkpoints.sqlite"


@pytest.fixture
def payload():
    state = json.loads((ROOT / "contracts/state.example.json").read_text())
    return {"mode": "scripted", "run_id": "sdk-test-run", "data_epoch": state["data_epoch"],
            "state_revision": state["revision"], "state": state}


def run(extension, payload):
    return asyncio.run(extension.handle("synthesize", payload))


def assert_contract(result):
    schema = json.loads((ROOT / "contracts/state.schema.json").read_text())
    Draft202012Validator({"$ref": "#/$defs/synthesis", "$defs": schema["$defs"]}).validate(result)


def configure(monkeypatch):
    monkeypatch.setenv("COMPANION_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("COMPANION_LLM_MODEL", "synthetic-test-model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-never-a-credential")


def test_no_key_and_disabled(checkpoint, payload):
    extension = module.SynthesisExtension(checkpoint)
    assert (extension.id, extension.version, extension.status) == ("synthesis", "1", "available")
    payload["mode"] = "disabled"
    result = run(extension, payload)
    assert result == {"mode": "disabled", "status": "skipped", "reason": "disabled", "engine": "deepagents", "drafts": []}
    payload["mode"] = "live"
    result = run(extension, payload)
    assert result["reason"] == "no_credentials"
    assert_contract(result)
    assert not checkpoint.exists()


def test_actual_sdk_delegation_files_and_shared_budget(checkpoint, payload, monkeypatch):
    before = copy.deepcopy(payload)
    budgets = []
    original = module._Budget

    class RecordedBudget(original):
        def __post_init__(self):
            pass

        def model(self, wire):
            if self not in budgets:
                budgets.append(self)
            assert "Synthetic Cafe" not in json.dumps(wire)
            assert "Synthetic demo preferences" not in json.dumps(wire)
            assert "demo-purchase-1" not in json.dumps(wire)
            return super().model(wire)

    monkeypatch.setattr(module, "_Budget", RecordedBudget)
    result = run(module.SynthesisExtension(checkpoint), payload)
    assert result["status"] == "completed", result
    assert result["drafts"] and result["drafts"][0]["card_ids"] == ["purchase_recurrence"]
    assert result["drafts"][0]["text"].startswith("Scripted coverage review:")
    assert payload == before
    assert len(budgets) == 1
    budget = budgets[0]
    assert budget.model_calls == 4
    assert budget.tool_calls == 3
    assert budget.executed == {"task", "write_file", "read_file"}
    assert budget.input_tokens <= 8000 and budget.output_reserved <= 1000
    assert checkpoint.is_file()
    assert not (checkpoint.parent / "coverage.json").exists()
    assert_contract(result)


def test_fresh_process_reuses_checkpoint(checkpoint, payload):
    first = run(module.SynthesisExtension(checkpoint), payload)
    assert first["status"] == "completed", first
    script = '''
import asyncio,json,socket,sys
from pathlib import Path
from companion.extensions import synthesis as m
socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network'))
p=json.loads(sys.stdin.read())
original=m._scripted_models
def empty_models(*args):
    parent,child=original(*args)
    parent.responses=[]
    child.responses=[]
    return parent,child
m._scripted_models=empty_models
print(json.dumps(asyncio.run(m.SynthesisExtension(Path(sys.argv[1])).handle('synthesize',p))))
'''
    env = {**os.environ, "PYTHONPATH": str(ROOT / "be"), "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run([sys.executable, "-c", script, str(checkpoint)], input=json.dumps(payload),
                               text=True, capture_output=True, check=True, env=env, cwd=ROOT, timeout=20)
    assert json.loads(completed.stdout) == first


@pytest.mark.parametrize("mode", ["scripted", "live"])
def test_live_evidence_never_reaches_graph(checkpoint, payload, monkeypatch, mode):
    configure(monkeypatch)
    payload["mode"] = mode
    payload["state"]["preferences"]["cloud_consent"] = True
    payload["state"]["observations"][0]["mode"] = "live"
    monkeypatch.setattr(module.llm, "create_chat_model", lambda: pytest.fail("Provider constructed for live evidence"))
    if mode == "scripted":
        with pytest.raises(module.UnsafeSynthesisInput) as error:
            run(module.SynthesisExtension(checkpoint), payload)
        assert error.value.status_code == 409
    else:
        assert run(module.SynthesisExtension(checkpoint), payload)["reason"] == "live_not_approved"
    assert not checkpoint.exists()


def test_false_fixture_flag_and_cloud_consent(checkpoint, payload, monkeypatch):
    configure(monkeypatch)
    payload["mode"] = "live"
    monkeypatch.setattr(module.llm, "create_chat_model", lambda: pytest.fail("Provider constructed before consent"))
    assert run(module.SynthesisExtension(checkpoint), payload)["reason"] == "cloud_consent_required"
    payload["state"]["preferences"]["cloud_consent"] = True
    payload["state"]["observations"][0]["source_id"] = "healthkit"
    assert run(module.SynthesisExtension(checkpoint), payload)["reason"] == "live_not_approved"


@pytest.mark.parametrize("content", ['not JSON', '{"selected":["missing"]}', '{"selected":["c0","c0"]}',
                                      '{"selected":["c0"],"text":"You have a disease"}', '{"drafts":[]}'])
def test_malformed_or_unapproved_model_output(checkpoint, payload, monkeypatch, content):
    original = module._scripted_models

    def altered(document, keys):
        parent, child = original(document, keys)
        parent.responses[-1] = AIMessage(content=content)
        return parent, child

    monkeypatch.setattr(module, "_scripted_models", altered)
    result = run(module.SynthesisExtension(checkpoint), payload)
    assert result["status"] == "failed" and result["reason"] == "invalid_output"
    assert not result["drafts"]
    assert_contract(result)


def test_child_review_is_required(checkpoint, payload, monkeypatch):
    original = module._scripted_models

    def altered(document, keys):
        parent, child = original(document, keys)
        child.responses = [AIMessage(content='{"reviewed":[]}')]
        return parent, child

    monkeypatch.setattr(module, "_scripted_models", altered)
    assert run(module.SynthesisExtension(checkpoint), payload)["reason"] == "invalid_output"


@pytest.mark.parametrize("change", ["dangling", "old", "refunded", "removed"])
def test_invalid_evidence_is_rejected(checkpoint, payload, change):
    state = payload["state"]
    observation = state["observations"][0]
    if change == "dangling":
        state["ledger"][0]["evidence"][0]["observation_id"] = "does-not-exist"
    elif change == "old":
        new = copy.deepcopy(observation)
        new["revision"] += 1
        new["supersedes"] = {"observation_id": observation["id"], "revision": observation["revision"]}
        state["observations"].append(new)
    elif change == "refunded":
        observation["purchase"]["is_refund"] = True
    else:
        observation["operation"] = "remove"
    assert run(module.SynthesisExtension(checkpoint), payload)["reason"] == "invalid_output"


@pytest.mark.parametrize("change", ["suppressed", "disabled", "revoked", "constraint"])
def test_preferences_and_revocation_filter_drafts(checkpoint, payload, change):
    state = payload["state"]
    if change == "suppressed":
        state["preferences"]["suppressed_scenario_ids"] = ["purchase_recurrence"]
    elif change == "disabled":
        state["preferences"]["food_suggestions"] = False
    elif change == "revoked":
        state["sources"][0]["consent"] = "revoked"
    else:
        state["ledger"][0]["suggestion"]["requires_spending"] = True
    result = run(module.SynthesisExtension(checkpoint), payload)
    assert result["status"] == "completed", result
    assert result["drafts"] == []


@pytest.mark.parametrize("name,value", [("MAX_MODEL_CALLS", 3), ("MAX_TOOL_CALLS", 2),
                                         ("MAX_INPUT_TOKENS", 20), ("MAX_OUTPUT_TOKENS", 40)])
def test_enforced_shared_budgets(checkpoint, payload, monkeypatch, name, value):
    monkeypatch.setattr(module, name, value)
    result = run(module.SynthesisExtension(checkpoint), payload)
    assert result["status"] == "failed" and result["reason"] == "invalid_output", result
    assert result["drafts"] == []


def test_oversize_response_and_disallowed_tool(checkpoint, payload, monkeypatch):
    original = module._scripted_models

    def altered(document, keys):
        parent, child = original(document, keys)
        parent.responses[0] = AIMessage(content="", tool_calls=[{
            "name": "execute", "args": {"command": "echo denied"}, "id": "bad", "type": "tool_call"}])
        return parent, child

    monkeypatch.setattr(module, "_scripted_models", altered)
    assert run(module.SynthesisExtension(checkpoint), payload)["reason"] == "invalid_output"


class _ControlledDeadline:
    """Expire only this extension's wait boundary, not the SDK's asyncio calls."""

    def __init__(self, entered=None):
        self.entered = entered
        self.task = None

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def wait_for(self, task, *, timeout):
        assert timeout == module.MAX_SECONDS == 30.0
        self.task = task
        if self.entered is not None:
            # This is a startup watchdog, not the simulated execution deadline.
            # Model-entry synchronization removes SQLite/SDK timing from the test.
            await asyncio.wait_for(self.entered.wait(), timeout=timeout)
        # Real asyncio cancellation and cleanup; no wall-clock race or timer sleep.
        return await asyncio.wait_for(task, timeout=0)


def test_timeout_cancels_graph(checkpoint, payload, monkeypatch):
    async def exercise():
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        original = module._scripted_models
        deadline = _ControlledDeadline(entered)

        def altered(document, keys):
            parent, child = original(document, keys)

            async def slow(self, *args, **kwargs):
                entered.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            monkeypatch.setattr(type(parent), "_agenerate", slow)
            return parent, child

        monkeypatch.setattr(module, "_scripted_models", altered)
        monkeypatch.setattr(module, "asyncio", deadline)
        extension = module.SynthesisExtension(checkpoint)
        result = await extension.handle("synthesize", payload)
        assert result["status"] == "failed" and result["reason"] == "timeout"
        assert entered.is_set() and cancelled.is_set()
        assert deadline.task.done() and deadline.task.cancelled()
        assert extension._task is None
        assert_contract(result)
        assert await extension.handle("clear", {}) == {"cleared": True}
        assert not list(checkpoint.parent.glob("checkpoints.sqlite*"))

    asyncio.run(exercise())


def test_timeout_before_execution_starts(checkpoint, payload, monkeypatch):
    async def exercise():
        entered = asyncio.Event()
        deadline = _ControlledDeadline()
        extension = module.SynthesisExtension(checkpoint)

        async def unexpected_execution(_payload):
            entered.set()
            pytest.fail("An already-expired deadline must cancel before execution starts")

        monkeypatch.setattr(extension, "_execute", unexpected_execution)
        monkeypatch.setattr(module, "asyncio", deadline)
        result = await extension.handle("synthesize", payload)
        assert result["status"] == "failed" and result["reason"] == "timeout"
        assert not entered.is_set()
        assert deadline.task.done() and deadline.task.cancelled()
        assert extension._task is None
        assert not checkpoint.exists()
        assert_contract(result)

    asyncio.run(exercise())


def test_clear_cancels_running_graph_and_deletes_sidecars(checkpoint, payload, monkeypatch):
    async def exercise():
        extension = module.SynthesisExtension(checkpoint)
        started = asyncio.Event()
        original = module._scripted_models

        def altered(document, keys):
            parent, child = original(document, keys)

            async def slow(self, *args, **kwargs):
                started.set()
                await asyncio.sleep(30)

            monkeypatch.setattr(type(parent), "_agenerate", slow)
            return parent, child

        monkeypatch.setattr(module, "_scripted_models", altered)
        pending = asyncio.create_task(extension.handle("synthesize", payload))
        await asyncio.wait_for(started.wait(), 5)
        assert await extension.handle("clear", {}) == {"cleared": True}
        assert (await pending)["reason"] == "stale_context"
        assert not list(checkpoint.parent.glob("checkpoints.sqlite*"))
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(checkpoint) + suffix).write_bytes(b"synthetic")
        assert await extension.handle("clear", {}) == {"cleared": True}
        assert not list(checkpoint.parent.glob("checkpoints.sqlite*"))

    asyncio.run(exercise())


def test_missing_sdk_and_stale_context(checkpoint, payload, monkeypatch):
    payload["state_revision"] += 1
    assert run(module.SynthesisExtension(checkpoint), payload)["reason"] == "stale_context"
    monkeypatch.setattr(module, "_sdk_available", lambda: False)
    extension = module.SynthesisExtension(checkpoint)
    assert extension.status == "not_configured"
    assert run(extension, payload)["reason"] == "internal_error"


def _enable_empty_healthkit(payload):
    state = payload["state"]
    state["preferences"]["enabled_source_ids"] = ["demo", "healthkit"]
    state["preferences"]["movement_restriction"] = False
    healthkit = next(source for source in state["sources"] if source["id"] == "healthkit")
    healthkit.update(enabled=True, consent="granted", status="empty", coverage="unknown")
    steps = next(card for card in state["ledger"] if card["id"] == "recorded_steps")
    steps.update(mode="live", status="insufficient_evidence", evidence=[], suggestion=None,
                 inferred=None, recorded_steps=None, days_with_records=0, coverage="unknown",
                 observed="EMPTY_LIVE_CARD_MUST_NOT_LEAVE")
    return steps


def test_scripted_allows_empty_healthkit_but_never_selects_it(checkpoint, payload, monkeypatch):
    _enable_empty_healthkit(payload)
    before = copy.deepcopy(payload)
    original = module._scripted_models

    def inspected(document, keys):
        assert json.loads(document) == {"cards": [{"key": "c0", "coverage": "partial", "records": 3}]}
        assert keys == ["c0"]
        return original(document, keys)

    monkeypatch.setattr(module, "_scripted_models", inspected)
    result = run(module.SynthesisExtension(checkpoint), payload)
    assert result["status"] == "completed", result
    assert [draft["card_ids"] for draft in result["drafts"]] == [["purchase_recurrence"]]
    candidates = module._candidates(payload["state"])
    assert [candidate["card_id"] for candidate in candidates] == ["purchase_recurrence"]
    with pytest.raises(module._InvalidOutput):
        module._validate_selection('{"selected":["c1"]}', candidates)
    assert payload == before
    assert_contract(result)


@pytest.mark.parametrize("mode", ["scripted", "live"])
@pytest.mark.parametrize("populated", ["evidence", "zero_steps", "recorded_day", "observed", "live_observation"])
def test_empty_healthkit_exception_does_not_admit_real_data(checkpoint, payload, monkeypatch, mode, populated):
    steps = _enable_empty_healthkit(payload)
    configure(monkeypatch)
    payload["mode"] = mode
    payload["state"]["preferences"]["cloud_consent"] = True
    if populated == "evidence":
        steps["evidence"] = [{"observation_id": "live-record", "revision": 1}]
    elif populated == "zero_steps":
        steps["recorded_steps"] = 0  # An explicitly recorded zero is still data.
    elif populated == "recorded_day":
        steps["days_with_records"] = 1
    elif populated == "observed":
        steps["status"] = "observed"
    else:
        payload["state"]["observations"][0]["mode"] = "live"
    monkeypatch.setattr(module.llm, "create_chat_model", lambda: pytest.fail("Real data reached provider construction"))
    if mode == "scripted":
        with pytest.raises(module.UnsafeSynthesisInput):
            run(module.SynthesisExtension(checkpoint), payload)
    else:
        assert run(module.SynthesisExtension(checkpoint), payload)["reason"] == "live_not_approved"
    assert not checkpoint.exists()


@pytest.mark.parametrize("empty_healthkit", [False, True])
@pytest.mark.parametrize("provider", ["openrouter", "requesty"])
def test_full_live_sdk_graph_with_mocked_http(checkpoint, payload, monkeypatch, provider, empty_healthkit):
    import httpx

    configure(monkeypatch)
    monkeypatch.setenv("COMPANION_LLM_PROVIDER", provider)
    monkeypatch.setenv("REQUESTY_API_KEY", "synthetic-never-a-credential")
    payload["mode"] = "live"
    payload["state"]["preferences"]["cloud_consent"] = True
    payload["state"]["preferences"]["notes"] = "PRIVATE_CONTEXT_MUST_NOT_LEAVE"
    if empty_healthkit:
        _enable_empty_healthkit(payload)
    seen = []
    factory = module.llm.create_chat_model

    async def exercise():
        def handler(request):
            body = json.loads(request.content)
            seen.append(body)
            serialized = json.dumps(body)
            assert "PRIVATE_CONTEXT_MUST_NOT_LEAVE" not in serialized
            assert "EMPTY_LIVE_CARD_MUST_NOT_LEAVE" not in serialized
            assert "demo-purchase" not in serialized
            assert "Synthetic Cafe" not in serialized
            assert "healthkit" not in serialized
            assert "shell" not in serialized and "execute" not in serialized
            assert body["max_completion_tokens"] == (350, 350, 150, 150)[len(seen) - 1]
            document = next(m["content"] for m in body["messages"] if m["role"] == "user")
            assert json.loads(document) == {"cards": [{"key": "c0", "coverage": "partial", "records": 3}]}
            def call(name, args, ident):
                return {"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            message = {"role": "assistant", "content": ""}
            finish = "tool_calls"
            if len(seen) == 1:
                message["tool_calls"] = [call("write_file", {"file_path": "/coverage.json", "content": document}, "w")]
            elif len(seen) == 2:
                message["tool_calls"] = [call("read_file", {"file_path": "/coverage.json"}, "r"),
                    call("task", {"description": document, "subagent_type": "coverage-review"}, "t")]
            else:
                message["content"] = '{"reviewed":["c0"]}' if len(seen) == 3 else '{"selected":["c0"]}'
                finish = "stop"
            return httpx.Response(200, json={"id": "mock", "object": "chat.completion", "created": 1,
                "model": "synthetic-test-model", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 30, "total_tokens": 70}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(module.llm, "create_chat_model", lambda: factory(http_async_client=client))
            result = await module.SynthesisExtension(checkpoint).handle("synthesize", payload)
            assert result["status"] == "completed", result
            assert result["drafts"][0]["text"].startswith("Coverage review:")
            assert_contract(result)

    asyncio.run(exercise())
    assert len(seen) == 4


@pytest.mark.parametrize("failure,reason", [("timeout", "timeout"), ("rate_limit", "internal_error")])
def test_live_transport_failures_are_bounded(checkpoint, payload, monkeypatch, failure, reason):
    import httpx

    configure(monkeypatch)
    payload["mode"] = "live"
    payload["state"]["preferences"]["cloud_consent"] = True
    factory = module.llm.create_chat_model
    calls = []

    def handler(request):
        calls.append(True)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        return httpx.Response(429, json={"error": {"message": "synthetic"}})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(module.llm, "create_chat_model", lambda: factory(http_async_client=client))
            result = await module.SynthesisExtension(checkpoint).handle("synthesize", payload)
            assert result["reason"] == reason, result
            assert result["drafts"] == []

    asyncio.run(exercise())
    assert len(calls) == 1


def test_both_eligible_cards_stay_within_budgets(checkpoint, payload):
    state = payload["state"]
    state["preferences"]["movement_restriction"] = False
    state["ledger"][1]["suggestion"]["status"] = "eligible"
    state["ledger"][1]["suggestion"]["reason"] = None
    result = run(module.SynthesisExtension(checkpoint), payload)
    assert result["status"] == "completed", result
    assert len(result["drafts"]) == 2


def test_oversized_model_response_is_rejected(checkpoint, payload, monkeypatch):
    original = module._scripted_models

    def altered(document, keys):
        parent, child = original(document, keys)
        parent.responses[0] = AIMessage(content="x" * 1001)
        return parent, child

    monkeypatch.setattr(module, "_scripted_models", altered)
    assert run(module.SynthesisExtension(checkpoint), payload)["reason"] == "invalid_output"


def test_bootstrap_process_and_delete_integration(checkpoint):
    from companion.bootstrap import build_service

    async def exercise():
        service = build_service(checkpoint.parent)
        try:
            await service.start()
            initial = await service.state()
            seeded = await service.mutate("/demo", {"schema_version": "1", "request_id": "seed-sdk",
                "data_epoch": initial["data_epoch"], "action": "seed", "fixture_set": "representative-v1", "consent_demo": True})
            result = await service.process({"schema_version": "1", "request_id": "process-sdk",
                "data_epoch": seeded["data_epoch"], "synthesis_mode": "scripted"})
            assert result["process"]["synthesis"]["status"] == "completed", result["process"]
            assert result["process"]["synthesis"]["drafts"]
            assert result["ledger"] == seeded["ledger"]
            assert result["preferences"] == seeded["preferences"]
            assert result["notification_intents"] == seeded["notification_intents"]
            assert_contract(result["process"]["synthesis"])
            assert checkpoint.exists()
            cleared = await service.mutate("/data", {"schema_version": "1", "request_id": "delete-sdk",
                "data_epoch": result["data_epoch"], "confirm": "delete_all"})
            assert cleared["observations"] == []
            assert not list(checkpoint.parent.glob("checkpoints.sqlite*"))
        finally:
            await service.close()

    asyncio.run(exercise())


def test_budget_cannot_be_reset_by_automatic_checkpoint_resume(checkpoint, payload, monkeypatch):
    original = module._scripted_models

    def altered(document, keys):
        parent, child = original(document, keys)
        child.responses = [AIMessage(content="invalid review")]
        return parent, child

    monkeypatch.setattr(module, "_scripted_models", altered)
    assert run(module.SynthesisExtension(checkpoint), payload)["status"] == "failed"
    monkeypatch.setattr(module, "_scripted_models", original)
    assert run(module.SynthesisExtension(checkpoint), payload)["status"] == "failed"
