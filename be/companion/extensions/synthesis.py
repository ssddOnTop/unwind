"""Optional, bounded Deep Agents coverage review. Never owns domain facts.

Only anonymous synthetic coverage counts enter the graph. Model output selects
from local, approved wording; it never supplies publishable medical prose.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from companion.connections import llm

MAX_SECONDS = 30.0
MAX_MODEL_CALLS = 4
MAX_TOOL_CALLS = 4
MAX_INPUT_TOKENS = 8000
MAX_OUTPUT_TOKENS = 1000
_VIRTUAL_PATH = "/coverage.json"
_REVIEW_TASK = "review_coverage"  # An operation token, never model-authored instructions.
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_TEXT = "These synthetic connected records may be incomplete. Missing observations remain unknown."


class UnsafeSynthesisInput(ValueError):
    """HTTP composition must map scripted non-fixture input to 409 invalid_request."""

    code = "invalid_request"
    status_code = 409


class _InvalidOutput(ValueError):
    pass


class _BudgetExceeded(_InvalidOutput):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _result(mode: str, status: str, reason: str | None = None, drafts=None) -> dict:
    return {"mode": mode, "status": status, "reason": reason,
            "engine": "deepagents", "drafts": drafts or []}


def _sdk_available() -> bool:
    try:
        return all(importlib.util.find_spec(m) is not None for m in (
            "deepagents", "langgraph.checkpoint.sqlite.aio", "langchain_core"))
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _synthetic(state: dict) -> bool:
    # Trust is anchored in the backend-only import path: public /observations
    # rejects Plaid uploads, and only the opted-in Sandbox coordinator grants its
    # source capability/consent. A caller-supplied fixture label is not authority.
    sources = {source["id"]: source for source in state.get("sources", [])}
    enabled = state.get("preferences", {}).get("enabled_source_ids", [])
    for observation in state.get("observations", []):
        if observation.get("mode") != "fixture":
            return False
        provenance = observation.get("provenance", {})
        if observation.get("source_id") == "demo":
            if provenance.get("adapter") != "demo_fixture" or provenance.get("method") != "synthetic":
                return False
        elif observation.get("source_id") == "plaid":
            source = sources.get("plaid", {})
            if (observation.get("kind") != "purchase" or observation.get("activity") is not None
                    or observation.get("time_precision") != "date" or observation.get("occurred_at") is not None
                    or set(provenance) != {"adapter", "adapter_version", "method", "record_id", "coverage"}
                    or provenance.get("adapter") != "plaid_sandbox"
                    or provenance.get("adapter_version") != "1"
                    or provenance.get("method") != "transactions_sync"
                    or provenance.get("coverage") not in {"unknown", "partial"}
                    or not isinstance(provenance.get("record_id"), str)
                    or not _ID.fullmatch(provenance["record_id"])
                    or source.get("capability") != "available" or source.get("consent") != "granted"
                    or source.get("enabled") is not True or "plaid" not in enabled
                    or source.get("authorization_requested") is not True
                    or source.get("read_access") != "not_applicable"):
                return False
        else:
            return False
    # Selecting HealthKit produces a live-mode abstention card even before any
    # samples exist. That empty card is not evidence and is never a candidate.
    return all(
        card.get("mode") == "fixture" or (
            card.get("mode") == "live"
            and card.get("id") == card.get("scenario_id") == "recorded_steps"
            and card.get("status") == "insufficient_evidence"
            and card.get("evidence") == []
            and card.get("suggestion") is None and card.get("inferred") is None
            and card.get("coverage") == "unknown"
            and card.get("recorded_steps") is None
            and card.get("days_with_records") == 0
            and card.get("purchase_count") is None
            and card.get("distinct_purchase_dates") is None
        )
        for card in state.get("ledger", [])
    )


def _candidates(state: dict) -> list[dict]:
    """Read references only. Never expose observations, notes, dates or raw IDs to AI."""
    observations = {(o["id"], o["revision"]): o for o in state["observations"]}
    superseded = {(o["supersedes"]["observation_id"], o["supersedes"]["revision"])
                  for o in state["observations"] if o.get("supersedes")}
    latest = {}
    for o in state["observations"]:
        latest[o["id"]] = max(latest.get(o["id"], 0), o["revision"])
    prefs = state["preferences"]
    sources = {s["id"]: s for s in state["sources"]}
    output = []
    seen = set()
    for card in state["ledger"]:
        cid = card["id"]
        if cid in seen:
            raise _InvalidOutput("Duplicate card")
        seen.add(cid)
        if card.get("mode") != "fixture" or cid not in {"purchase_recurrence", "recorded_steps"}:
            continue
        if (card.get("scenario_id") != cid or card.get("status") == "insufficient_evidence"
                or cid in prefs["suppressed_scenario_ids"]):
            continue
        suggestion = card.get("suggestion")
        if not suggestion or suggestion.get("status") != "eligible":
            continue
        if cid == "purchase_recurrence" and not prefs["food_suggestions"]:
            continue
        if cid == "recorded_steps" and (not prefs["activity_suggestions"] or prefs["movement_restriction"]):
            continue
        if any(suggestion.get(req) and prefs.get(pref) for req, pref in (
            ("requires_kitchen", "no_kitchen"), ("requires_spending", "no_spending"),
            ("requires_movement", "movement_restriction"))):
            continue
        refs = card["evidence"]
        if not refs or len(refs) > 500:
            continue
        valid_refs = []
        card_sources = set()
        for ref in refs:
            if set(ref) != {"observation_id", "revision"}:
                raise _InvalidOutput("Invalid reference")
            key = (ref["observation_id"], ref["revision"])
            observation = observations.get(key)
            if (not observation or key in superseded or latest[key[0]] != key[1]
                    or observation.get("operation") != "upsert"
                    or observation.get("lifecycle") not in {"posted", "recorded"}
                    or (observation.get("purchase") or {}).get("is_refund")):
                raise _InvalidOutput("Dangling or inactive reference")
            expected_kind = "purchase" if cid == "purchase_recurrence" else "activity"
            if observation.get("kind") != expected_kind or observation.get("mode") != card.get("mode"):
                raise _InvalidOutput("Incompatible card evidence")
            card_sources.add(observation["source_id"])
            if len(card_sources) != 1:
                raise _InvalidOutput("Mixed source card")
            source = sources.get(observation["source_id"], {})
            if not source.get("enabled") or source.get("consent") != "granted":
                break
            if key in {(r["observation_id"], r["revision"]) for r in valid_refs}:
                raise _InvalidOutput("Duplicate reference")
            valid_refs.append(copy.deepcopy(ref))
        else:
            coverage = card["coverage"]
            if coverage not in {"unknown", "partial", "complete"}:
                raise _InvalidOutput("Invalid coverage")
            output.append({"key": f"c{len(output)}", "card_id": cid,
                           "evidence": valid_refs, "coverage": coverage})
    return output


@dataclass
class _Budget:
    """One shared budget for all graph/subagent calls, reserved before awaiting.

    UTF-8/ASCII JSON bytes + framing are a deliberately conservative upper bound
    for the supported byte-tokenized chat APIs, not an optimistic word estimate.
    Provider output allowance is reserved globally, even when actual usage is less.
    """
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_reserved: int = 0
    executed: set[str] = field(default_factory=set)
    violation: bool = False

    def model(self, wire: dict) -> int:
        cost = len(_json(wire).encode("utf-8")) + 64 * max(1, len(wire.get("messages", [])))
        # File/delegation arguments need more headroom than the two JSON verdicts.
        allocations = (350, 350, 150, 150)
        allowance = min(allocations[min(self.model_calls, 3)], MAX_OUTPUT_TOKENS - self.output_reserved)
        if (self.model_calls >= MAX_MODEL_CALLS or allowance <= 0
                or self.input_tokens + cost > MAX_INPUT_TOKENS):
            self.violation = True
            raise _BudgetExceeded("Model budget")
        self.model_calls += 1
        self.input_tokens += cost
        self.output_reserved += allowance
        return allowance

    def tool(self, name: str) -> None:
        if self.tool_calls >= MAX_TOOL_CALLS:
            self.violation = True
            raise _BudgetExceeded("Tool budget")
        self.tool_calls += 1


def _checked_document(state: dict, document: str) -> str:
    """Fail closed before any model sees mutated, foreign, or additional files."""
    files = state.get("files", {})
    if not isinstance(files, dict) or set(files) != {_VIRTUAL_PATH}:
        raise _InvalidOutput("Unexpected virtual files")
    virtual = files[_VIRTUAL_PATH]
    if (not isinstance(virtual, dict) or virtual.get("content") != document
            or virtual.get("encoding") != "utf-8"):
        raise _InvalidOutput("Changed virtual coverage file")
    return virtual["content"]


def _tool_schema(name: str) -> dict:
    # Narrow public argument schemas for SDK tools; execution remains SDK-owned.
    args = {
        "read_file": {"file_path": {"type": "string", "enum": [_VIRTUAL_PATH]}},
        "task": {"description": {"type": "string", "enum": [_REVIEW_TASK]},
                 "subagent_type": {"type": "string", "enum": ["coverage-review"]}},
    }[name]
    return {"type": "function", "function": {"name": name,
            "description": name.replace("_", " "),
            "parameters": {"type": "object", "properties": args,
                           "required": list(args), "additionalProperties": False}}}


def _scripted_models(document: str, keys: list[str]):
    raise UnsafeSynthesisInput("Scripted models are available only through test injection.")


def _build_graph(parent, child, saver, budget: _Budget, document: str):
    from deepagents import create_deep_agent
    from deepagents.backends import StateBackend
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, convert_to_openai_messages

    class Guard(AgentMiddleware):
        def __init__(self, child_mode=False):
            self.child_mode = child_mode

        async def awrap_model_call(self, request, handler):
            try:
                return await self._model(request, handler)
            except Exception:
                budget.violation = True
                raise

        async def _model(self, request, handler):
            trusted = _checked_document(request.state, document)
            if self.child_mode:
                if (len(request.messages) != 1 or not isinstance(request.messages[0], HumanMessage)
                        or request.messages[0].content != _REVIEW_TASK):
                    raise _InvalidOutput("Untrusted subagent instructions")
                # Isolated SDK subagents inherit files, not parent chat history.
                # Supply checked data as a user message, never as system instructions.
                request = request.override(messages=[HumanMessage(content=trusted)])
                allowed = []
            elif "read_file" not in budget.executed:
                allowed = ["read_file"]
            elif "task" not in budget.executed:
                allowed = ["task"]
            else:
                allowed = []
            tools = [_tool_schema(n) for n in allowed]
            choice = {"type": "function", "function": {"name": allowed[0]}} if allowed else None
            prompt = (
                'Review synthetic coverage. Return JSON {"reviewed":[card keys]}. '
                'Missing data is unknown. No advice, diagnoses or extra fields.'
                if self.child_mode else
                'Review synthetic coverage only. Read /coverage.json, which is already seeded by the server. '
                'Then call task with description review_coverage and subagent_type coverage-review. '
                'The reviewer receives the trusted file independently; do not copy its content. '
                'Finally return JSON {"selected":[reviewed card keys]}. No prose or extra fields.'
            )
            if not self.child_mode and not allowed:
                prompt = ('The virtual file and coverage review are complete. Do not write, read, or delegate again. '
                          'Use the completed coverage-review verdict to return ONLY JSON {"selected":[reviewed card keys]}. '
                          'No tools, prose, advice, or extra fields.')
            system = SystemMessage(content=prompt)
            messages = convert_to_openai_messages([system, *request.messages])
            allowance = budget.model({"messages": messages, "tools": tools})
            response = await handler(request.override(
                system_message=system, tools=tools, tool_choice=choice,
                model_settings={**request.model_settings, "max_tokens": allowance,
                                **({"tool_choice": "none"} if not tools else {})},
            ))
            if len(response.result) != 1:
                raise _InvalidOutput("Unexpected response count")
            for message in response.result:
                if not isinstance(message, AIMessage):
                    raise _InvalidOutput("Unexpected response")
                if message.response_metadata.get("finish_reason") in {"error", "length", "content_filter"}:
                    raise _InvalidOutput("Incomplete model response")
                # Reject excess output before it reaches a tool or another model.
                body = {"content": message.content, "tool_calls": message.tool_calls}
                size = len(_json(body).encode())
                usage = message.usage_metadata
                output_tokens = usage.get("output_tokens") if usage else None
                if isinstance(output_tokens, int) and output_tokens >= 0:
                    if output_tokens > allowance:
                        raise _BudgetExceeded("Output usage")
                    if size > 16384:
                        raise _BudgetExceeded("Response size")
                elif size > allowance:
                    # Scripted/unknown-usage responses retain the conservative cap.
                    raise _BudgetExceeded("Response size")
                if message.invalid_tool_calls:
                    raise _InvalidOutput("Invalid tool arguments")
                if len(message.tool_calls) != (1 if allowed else 0):
                    raise _InvalidOutput("Unexpected tool call count")
                for call in message.tool_calls:
                    if call["name"] not in allowed:
                        raise _InvalidOutput("Disallowed tool")
                if self.child_mode:
                    verdict = json.loads(message.content)
                    if not isinstance(verdict, dict) or set(verdict) != {"reviewed"}:
                        raise _InvalidOutput("Malformed coverage review")
                    _validate_selection(_json({"selected": verdict["reviewed"]}), json.loads(trusted)["cards"])
            _checked_document(request.state, document)
            return response

        async def awrap_tool_call(self, request, handler):
            try:
                return await self._tool(request, handler)
            except Exception:
                budget.violation = True
                raise

        async def _tool(self, request, handler):
            call = request.tool_call
            name, args = call["name"], call["args"]
            if self.child_mode:
                raise _InvalidOutput("Child tool denied")
            _checked_document(request.state, document)
            expected = {
                "read_file": {"file_path": _VIRTUAL_PATH},
                "task": {"description": _REVIEW_TASK, "subagent_type": "coverage-review"},
            }.get(name)
            if (expected is None or args != expected or name in budget.executed
                    or (name == "task" and "read_file" not in budget.executed)):
                raise _InvalidOutput("Tool boundary")
            budget.tool(name)
            response = await handler(request)
            if getattr(response, "status", None) == "error":
                raise _InvalidOutput("Tool execution failed")
            _checked_document(request.state, document)
            budget.executed.add(name)
            return response

    return create_deep_agent(
        model=parent, backend=StateBackend(), checkpointer=saver,
        middleware=[Guard()],
        subagents=[{"name": "coverage-review", "description": "Review evidence coverage, not health.",
                    "system_prompt": "Review coverage only.", "model": child, "tools": [],
                    "middleware": [Guard(child_mode=True)], "mode": "isolated"}],
    )


def _validate_selection(content: Any, candidates: list[dict]) -> list[str]:
    if not isinstance(content, str) or len(content) > 1000:
        raise _InvalidOutput("Malformed response")
    try:
        value = json.loads(content)
    except (ValueError, TypeError) as exc:
        raise _InvalidOutput("Malformed JSON") from exc
    if not isinstance(value, dict) or set(value) != {"selected"}:
        raise _InvalidOutput("Unexpected response fields")
    selected = value["selected"]
    keys = {c["key"] for c in candidates}
    if (not isinstance(selected, list) or len(selected) > len(keys)
            or any(not isinstance(k, str) or k not in keys for k in selected)
            or len(set(selected)) != len(selected)):
        raise _InvalidOutput("Dangling selection")
    return selected


class SynthesisExtension:
    id = "synthesis"
    version = "1"

    def __init__(self, checkpoint_path: Path):
        self.checkpoint_path = Path(checkpoint_path)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._generation = 0
        self._clearing = False

    @property
    def status(self) -> str:
        # Scripted SDK execution is available without optional live credentials.
        return "available" if _sdk_available() else "not_configured"

    async def handle(self, operation: str, payload: dict) -> dict:
        if operation == "clear":
            self._clearing = True
            self._generation += 1
            try:
                if self._task is not None:
                    self._task.cancel()
                    await asyncio.gather(self._task, return_exceptions=True)
                async with self._lock:
                    for suffix in ("", "-wal", "-shm", "-journal"):
                        self.checkpoint_path.with_name(self.checkpoint_path.name + suffix).unlink(missing_ok=True)
                return {"cleared": True}
            finally:
                self._clearing = False
        if operation != "synthesize":
            raise ValueError("Unsupported synthesis operation")
        mode = payload.get("mode")
        if mode not in {"disabled", "scripted", "live"}:
            raise ValueError("Unsupported synthesis mode")
        if mode == "disabled":
            return _result(mode, "skipped", "disabled")
        if mode == "live" and not llm.is_configured():
            return _result(mode, "skipped", "no_credentials")
        state = payload.get("state", {})
        if mode == "live" and not state.get("preferences", {}).get("cloud_consent"):
            return _result(mode, "skipped", "cloud_consent_required")
        if not _synthetic(state):
            if mode == "scripted":
                raise UnsafeSynthesisInput("Scripted synthesis requires trusted, consented fixture evidence.")
            return _result(mode, "skipped", "live_not_approved")
        if not _sdk_available():
            return _result(mode, "failed", "internal_error")
        if self._clearing:
            return _result(mode, "failed", "stale_context")
        if self._task is not None:
            return _result(mode, "failed", "internal_error")
        generation = self._generation
        task = None
        try:
            # Copy before the first await; graph cannot mutate the caller's state.
            frozen = copy.deepcopy(payload)
            task = asyncio.create_task(self._execute(frozen))
            self._task = task
            answer = await asyncio.wait_for(task, timeout=MAX_SECONDS)
            if generation != self._generation:
                return _result(mode, "failed", "stale_context")
            return answer
        except asyncio.CancelledError:
            if generation != self._generation:
                return _result(mode, "failed", "stale_context")
            raise
        except TimeoutError:
            return _result(mode, "failed", "timeout")
        except (_InvalidOutput, KeyError, TypeError, ValueError):
            return _result(mode, "failed", "invalid_output")
        except Exception as error:
            # SDK/provider details can contain prompts and credentials: never log.
            return _result(mode, "failed", "timeout" if llm.is_timeout(error) else "internal_error")
        finally:
            if self._task is task:
                self._task = None

    async def _execute(self, payload: dict) -> dict:
        from deepagents.backends.utils import create_file_data
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from langsmith import tracing_context

        state, mode = payload["state"], payload["mode"]
        if (payload["data_epoch"] != state["data_epoch"]
                or type(payload["state_revision"]) is not int
                or payload["state_revision"] != state["revision"]):
            return _result(mode, "failed", "stale_context")
        if not isinstance(payload["run_id"], str) or not _ID.fullmatch(payload["run_id"]):
            raise _InvalidOutput("Invalid run identifier")
        if len(_json(state).encode()) > 1048576:
            raise _BudgetExceeded("Snapshot too large")
        candidates = _candidates(state)
        document = _json({"cards": [{"key": c["key"], "coverage": c["coverage"],
                                     "records": len(c["evidence"])} for c in candidates]})
        keys = [c["key"] for c in candidates]
        if mode == "scripted":
            parent, child = _scripted_models(document, keys)
        else:
            parent = llm.create_chat_model()
            if parent is None:
                return _result(mode, "skipped", "no_credentials")
            child = parent
        # Fingerprint all local inputs without placing those inputs in SDK state.
        digest = hashlib.sha256(_json({"flow": "seeded-v1", "request": payload}).encode()).hexdigest()
        config = {"configurable": {"thread_id": digest}, "recursion_limit": 12, "callbacks": []}
        budget = _Budget()
        async with self._lock:
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            with tracing_context(enabled=False):
                async with AsyncSqliteSaver.from_conn_string(str(self.checkpoint_path)) as saver:
                    graph = _build_graph(parent, child, saver, budget, document)
                    previous = await graph.aget_state(config)
                    if previous.values:
                        # Only completed runs are reusable; don't replenish the budget
                        # of a partially failed checkpoint by automatically resuming.
                        if previous.next:
                            raise _InvalidOutput("Interrupted checkpoint")
                        values = previous.values
                    else:
                        values = await graph.ainvoke(
                            {"messages": [{"role": "user", "content": "Review the seeded coverage file."}],
                             "files": {_VIRTUAL_PATH: create_file_data(document)}},
                            config, durability="sync")
                        if budget.executed != {"task", "read_file"}:
                            raise _InvalidOutput("Required SDK review not executed")
                    if budget.violation:
                        raise _InvalidOutput("SDK boundary violation")
                    _checked_document(values, document)
                    selected = _validate_selection(values["messages"][-1].content, candidates)
                    tool_messages = [m for m in values["messages"] if m.type == "tool"]
                    if [m.name for m in tool_messages] != ["read_file", "task"]:
                        raise _InvalidOutput("Missing completed review")
                    if any(getattr(m, "status", None) == "error" for m in tool_messages):
                        raise _InvalidOutput("Failed review")
                    reviews = [m.content for m in tool_messages if m.name == "task"]
                    reviewed = json.loads(reviews[-1])
                    if not isinstance(reviewed, dict) or set(reviewed) != {"reviewed"}:
                        raise _InvalidOutput("Malformed coverage review")
                    reviewed_keys = _validate_selection(_json({"selected": reviewed["reviewed"]}), candidates)
                    if not set(selected) <= set(reviewed_keys):
                        raise _InvalidOutput("Unreviewed selection")
        drafts = []
        for candidate in candidates:
            if candidate["key"] in selected:
                drafts.append({"id": f"synthesis:{digest[:20]}:{candidate['key']}",
                               "text": ("Scripted coverage review: " if mode == "scripted" else "Coverage review: ") + _TEXT,
                               "card_ids": [candidate["card_id"]],
                               "evidence": candidate["evidence"]})
        return _result(mode, "completed", drafts=drafts)
