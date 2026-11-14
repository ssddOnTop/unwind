"""Application transaction orchestration. HTTP and extension composition stay elsewhere."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from .contracts import DomainError, RULES, validate
from .domain import defaults, instant, iso, key, preference_update, prune, refkey, synthesis, utcnow, zone
from .ingestion import ingest


class CompanionService:
    def __init__(self, registry, scheduler, clock=utcnow, synthesis_timeout=30):
        self.registry, self.scheduler, self.clock = registry, scheduler, clock
        self.synthesis_timeout = synthesis_timeout
        self.lock = asyncio.Lock()
        self.document = {}
        self.synthesis_task = None
        self.required_failure = False

    @staticmethod
    def demo_only(doc):
        state = doc.get("state", {})
        return not any(o["mode"] == "live" or o["source_id"] != "demo"
                       for o in state.get("observations", [])) and not any(
            s["id"] != "demo" and (s["enabled"] or s["consent"] != "not_requested")
            for s in state.get("sources", []))

    def now(self, document=None):
        doc = document or self.document
        frozen = doc.get("meta", {}).get("fixture_session") and self.demo_only(doc)
        return instant(RULES["demo"]["now_utc"]) if frozen else self.clock()

    async def evaluate(self, doc):
        # Persist the transition: pruning or disconnecting live sources must not
        # reactivate the frozen demo clock. now() also protects legacy documents
        # before startup retention runs and this migration is committed.
        if not self.demo_only(doc):
            doc["meta"]["fixture_session"] = False
        s = doc["state"]
        s["evaluation_date"] = self.now(doc).astimezone(zone(s["preferences"]["timezone"])).date().isoformat()
        doc["state"] = (await self.registry.dispatch("scenarios", "evaluate", {"state": s}))["state"]
        plan = await self.scheduler.handle("plan", {"state": doc["state"], "issued": doc["meta"]["issued"], "now": iso(self.now(doc))})
        doc["state"]["notification_intents"] = plan["intents"]
        doc["meta"]["issued"] = plan["issued"]

    async def commit(self, doc, receipt=None, erase=False, recover=False, bump=True):
        if bump:
            doc["state"]["revision"] += 1
            doc["state"]["generated_at"] = iso(self.clock())
        validate("state", doc["state"])
        if receipt is not None and receipt["response"] is not None:
            if receipt["route"] == "/observations":
                receipt["response"]["state"] = deepcopy(doc["state"])
            else:
                receipt["response"] = deepcopy(doc["state"])
        await self.registry.dispatch("persistence", "commit", {
            "document": doc, "receipt": receipt, "erase_receipts": erase,
            "recover_pending": recover,
            "receipt_cutoff": iso(self.clock() - timedelta(days=90))})
        self.document = deepcopy(doc)

    async def start(self):
        async with self.lock:
            loaded = await self.registry.dispatch("persistence", "load", {})
            if not loaded:
                loaded = {"state": defaults(self.clock()), "meta": {"fixture_session": False, "issued": []}}
                await self.evaluate(loaded)
                await self.commit(loaded, bump=False)
                return
            self.document = deepcopy(loaded)
            s = loaded["state"]
            interrupted = s["process"]["status"] == "running"
            if interrupted:
                s["process"].update(status="failed", completed_at=iso(self.clock()))
                s["process"]["synthesis"] = synthesis(s["process"]["synthesis"]["mode"], "failed", "internal_error")
                await self.clear_checkpoints()
            erased = prune(s, self.now(loaded))
            if erased:
                self.invalidate(s)
                await self.clear_checkpoints()
            await self.evaluate(loaded)
            if loaded != self.document or interrupted:
                await self.commit(loaded, erase=erased, recover=interrupted)

    async def close(self):
        await self.cancel_synthesis()
        await self.registry.dispatch("persistence", "close", {})

    async def clear_checkpoints(self):
        result = await self.registry.dispatch("synthesis", "clear", {})
        if result != {"cleared": True}:
            raise RuntimeError("Checkpoint erasure failed")

    async def cancel_synthesis(self):
        task = self.synthesis_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @staticmethod
    def invalidate(state):
        result = state["process"]["synthesis"]
        if result["drafts"]:
            state["process"]["synthesis"] = synthesis(result["mode"], "skipped", "stale_context")

    async def housekeep(self):
        async with self.lock:
            doc = deepcopy(self.document)
            erased = prune(doc["state"], self.now(doc))
            await self.evaluate(doc)
            if doc != self.document:
                self.invalidate(doc["state"])
                if erased:
                    await self.cancel_synthesis()
                    await self.clear_checkpoints()
                await self.commit(doc, erase=erased)

    async def state(self):
        async with self.lock:
            return deepcopy(self.document["state"])

    def receipt(self, route, body):
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        return {"epoch": body["data_epoch"], "route": route, "request_id": body["request_id"],
                "digest": digest, "response": None, "created_at": iso(self.clock())}

    async def replay(self, receipt):
        cached = await self.registry.dispatch("persistence", "receipt", receipt)
        if cached:
            if cached["digest"] != receipt["digest"]:
                raise DomainError("idempotency_conflict", 409)
            if cached["response"] is None:
                raise DomainError("busy", 409)
            return cached["response"]
        if receipt["epoch"] != self.document["state"]["data_epoch"]:
            raise DomainError("epoch_mismatch", 409, "Refresh state before submitting new mutations.")
        return None

    async def mutate(self, route, body):
        if route == "/process":
            return await self.process(body)
        receipt = self.receipt(route, body)
        async with self.lock:
            replay = await self.replay(receipt)
            if replay is not None:
                return replay
            doc = deepcopy(self.document)
            s = doc["state"]
            erased = prune(s, self.now(doc))
            await self.evaluate(doc)
            s = doc["state"]
            counts = None
            if route == "/observations":
                counts = await ingest(self.registry, s, body)
            elif route == "/preferences":
                preference_update(s, body["preferences"])
            elif route == "/feedback":
                doc["state"] = (await self.registry.dispatch("feedback", "apply", {
                    "state": s, "request": body, "created_at": iso(self.clock())}))["state"]
            elif route == "/demo":
                result = await self.registry.dispatch("demo", body["action"], {
                    "state": s, "fixture_session": doc["meta"]["fixture_session"]})
                if result["changed"]:
                    doc["state"] = result["state"]
                    doc["state"]["revision"] = s["revision"]
                    doc["meta"] = {"fixture_session": True, "issued": []}
                    if body["action"] == "reset":
                        doc["state"].update(data_epoch=str(uuid4()), revision=0)
                        erased = True
            elif route == "/data":
                doc = {"state": defaults(self.clock()), "meta": {"fixture_session": False, "issued": []}}
                erased = True
            else:
                raise DomainError("not_found", 404)
            await self.evaluate(doc)
            changed = doc != self.document
            if changed:
                self.invalidate(doc["state"])
            if erased:
                await self.cancel_synthesis()
                await self.clear_checkpoints()
            elif changed and doc["state"]["process"]["status"] != "running":
                await self.clear_checkpoints()
            receipt["response"] = {"schema_version": "1", **counts, "state": doc["state"]} if counts is not None else doc["state"]
            await self.commit(doc, receipt, erase=erased, bump=changed)
            return deepcopy(receipt["response"])

    async def process(self, body):
        receipt = self.receipt("/process", body)
        async with self.lock:
            replay = await self.replay(receipt)
            if replay is not None:
                return replay
            if self.document["state"]["process"]["status"] == "running":
                raise DomainError("busy", 409)
            doc = deepcopy(self.document)
            mode = body["synthesis_mode"]
            if mode == "scripted" and any(o["mode"] == "live" for o in doc["state"]["observations"]):
                raise DomainError("invalid_request", 409)
            erased = prune(doc["state"], self.now(doc))
            if erased:
                await self.clear_checkpoints()
            try:
                await self.evaluate(doc)
            except Exception:
                self.required_failure = True
                doc = deepcopy(self.document)
                s = doc["state"]
                s["process"] = {"run_id": str(uuid4()), "status": "failed", "started_at": iso(self.clock()),
                    "completed_at": iso(self.clock()), "input_state_revision": s["revision"],
                    "ledger_status": "failed", "synthesis": synthesis(mode, "failed", "internal_error")}
                receipt["response"] = s
                await self.commit(doc, receipt)
                return deepcopy(receipt["response"])
            self.required_failure = False
            s = doc["state"]
            admission_revision = s["revision"] + 1
            epoch = s["data_epoch"]
            run_id = str(uuid4())
            s["process"] = {"run_id": run_id, "status": "running", "started_at": iso(self.clock()),
                "completed_at": None, "input_state_revision": admission_revision, "ledger_status": "completed",
                "synthesis": synthesis(mode, "running")}
            await self.commit(doc, receipt, erase=erased)
            payload = {"mode": mode, "run_id": run_id, "data_epoch": epoch,
                       "state_revision": admission_revision, "state": deepcopy(doc["state"])}
            self.synthesis_task = asyncio.create_task(self.registry.dispatch("synthesis", "synthesize", deepcopy(payload)))
            task = self.synthesis_task
        # No application mutation lock or SQLite transaction is held across model work.
        try:
            result = await asyncio.wait_for(task, timeout=self.synthesis_timeout)
            validate("synthesis", result)
            if result["mode"] != mode or result["status"] not in {"completed", "skipped", "failed"}:
                raise DomainError()
            if result["status"] != "completed" and result["drafts"]:
                raise DomainError()
            self.validate_drafts(result, payload["state"])
        except asyncio.TimeoutError:
            result = synthesis(mode, "failed", "timeout")
        except DomainError:
            result = synthesis(mode, "failed", "invalid_output")
        except asyncio.CancelledError:
            result = synthesis(mode, "failed", "internal_error")
        except Exception:
            result = synthesis(mode, "failed", "internal_error")
        async with self.lock:
            if self.document["state"]["data_epoch"] != epoch:
                return deepcopy(self.document["state"])
            doc = deepcopy(self.document)
            s = doc["state"]
            if s["revision"] != admission_revision:
                result = synthesis(mode, "skipped", "stale_context")
            try:
                self.validate_drafts(result, s)
            except DomainError:
                result = synthesis(mode, "failed", "invalid_output")
            if mode == "disabled":
                result = synthesis(mode, "skipped", "disabled")
            elif mode == "live" and result["reason"] != "no_credentials":
                if not s["preferences"]["cloud_consent"]:
                    result = synthesis(mode, "skipped", "cloud_consent_required")
                elif any(o["mode"] == "live" for o in s["observations"]):
                    result = synthesis(mode, "skipped", "live_not_approved")
            if result["status"] != "completed":
                try:
                    await self.clear_checkpoints()
                except Exception:
                    # Optional provider cleanup failures cannot break deterministic processing.
                    # Privacy deletion itself still fails closed if cleanup cannot be verified.
                    result = synthesis(mode, "failed", "internal_error")
            s["process"].update(status="completed", completed_at=iso(self.clock()), synthesis=result)
            receipt["response"] = s
            await self.commit(doc, receipt)
            return deepcopy(receipt["response"])

    @staticmethod
    def validate_drafts(result, state):
        cards = {c["id"]: c for c in state["ledger"]}
        for draft in result["drafts"]:
            if not draft["card_ids"] or any(cid not in cards for cid in draft["card_ids"]):
                raise DomainError()
            evidence = {refkey(r) for cid in draft["card_ids"] for r in cards[cid]["evidence"]}
            if any(refkey(r) not in evidence for r in draft["evidence"]):
                raise DomainError()
            if any(cid in state["preferences"]["suppressed_scenario_ids"] or
                   (cards[cid]["suggestion"] is not None and cards[cid]["suggestion"]["status"] != "eligible")
                   for cid in draft["card_ids"]):
                raise DomainError()
            observations = {key(o): o for o in state["observations"]}
            sources = {s["id"]: s for s in state["sources"]}
            for r in draft["evidence"]:
                observation = observations.get(refkey(r))
                if observation is None:
                    raise DomainError()
                source = sources[observation["source_id"]]
                if not source["enabled"] or source["consent"] != "granted":
                    raise DomainError()
