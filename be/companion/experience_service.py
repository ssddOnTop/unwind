"""Experience transaction adapter, scoped exclusively to the injected service instance."""
from copy import deepcopy
import hashlib
import json
from uuid import uuid4
from .contracts import DomainError
from .domain import iso, zone
from .experience_contracts import validate_experience


class ExperienceCoordinator:
    def __init__(self, service):
        self.service = service

    def payload(self, doc):
        service = self.service
        state = service.eligible_state(doc)
        healthkit = next(s for s in state["sources"] if s["id"] == "healthkit")
        return {"state": deepcopy(state), "answers": deepcopy(doc["meta"].get("experience_answers", [])),
                "now": iso(service.clock()),
                # Keep the legacy wire window bounded, never truncate stored history.
                "inference_history": deepcopy(doc["meta"].get("inference_history", [])[-128:]),
                "as_of_date": service.now(doc).astimezone(zone(state["preferences"]["timezone"])).date().isoformat(),
                "review_ready": bool(doc["meta"].get("experience_review_ready")) and not service.required_failure,
                "selected_sources": {"purchase_recurrence": "plaid" if doc["meta"].get("plaid_connected") else "demo",
                    "recorded_steps": "healthkit" if healthkit["enabled"] and healthkit["consent"] == "granted" else "demo"}}

    async def reconcile(self, doc):
        result = await self.service.registry.dispatch("experience", "clean", self.payload(doc))
        doc["meta"]["experience_answers"] = result["answers"]
        return result["removed"]

    async def capture_history(self, doc):
        """Persist only actual interpretations, atomically with their source state.

        Existing documents have no trustworthy inference time. Their initial
        projection is explicitly undated; neither generated_at nor refresh time
        is used to backfill it. Later semantic changes receive the commit clock.
        """
        meta = doc["meta"]
        initialized = meta.get("inference_history_initialized", False)
        legacy = bool(self.service.document) and not initialized
        projection = await self.service.registry.dispatch("experience", "project", self.payload(doc))
        value = projection["experience"]
        # Raw-record expiry/corrections must not erase completed interpretations.
        # Explicit /data deletion and reset create a fresh document instead.
        history = deepcopy(meta.get("inference_history", []))
        scope = projection.get("question_scope")
        fingerprint = None
        if scope and (value["visit"]["inferred_habits"] or value["concern"]["status"] == "corrected"):
            refs = {(r["observation_id"], r["revision"]) for r in projection["inference_evidence"]}
            rows = [o for o in self.service.eligible_state(doc)["observations"]
                    if (o["id"], o["revision"]) in refs and o["source_id"] != "demo"]
            if rows:
                evidence = [{"observation_id": o["id"], "revision": o["revision"],
                    "date": o["local_date"], "source": "Plaid Sandbox" if o["source_id"] == "plaid" else "Apple Health",
                    "summary": "Restaurant-category purchase; not proof of consumption."}
                    for o in sorted(rows, key=lambda o: (o["local_date"], o["id"]))]
                corrected = value["concern"]["status"] == "corrected"
                candidate = {"title": value["concern"]["title"],
                    "inference": value["concern"]["summary"],
                    "explanation": ("You reported these purchases were not mostly for you; the personal interpretation was removed." if corrected else
                        f"Restaurant-category purchases recur across {value['concern']['distinct_dates']} dates over {value['concern']['span_days']} days. Purchases do not confirm meals or diet quality."),
                    "status": "corrected" if corrected else "unconfirmed",
                    "method": "Deterministic rule", "rule_version": next(c["rule_version"] for c in doc["state"]["ledger"] if c["scenario_id"] == "purchase_recurrence"),
                    "context": value["visit"]["confirmed_context"], "evidence": evidence}
                fingerprint = hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()
                if fingerprint != meta.get("inference_history_current"):
                    history.append(dict(candidate, id=str(uuid4()), inferred_at=None if legacy else iso(self.service.clock())))
        meta["inference_history"] = history
        meta["inference_history_current"] = fingerprint
        meta["inference_history_initialized"] = True

    async def get(self):
        await self.service.housekeep()
        async with self.service.lock:
            result = await self.service.registry.dispatch("experience", "project", self.payload(self.service.document))
            validate_experience("experience", result["experience"])
            return result["experience"]

    async def answer(self, body):
        service = self.service
        # Ordinary housekeeping refreshes date/coverage before optimistic validation.
        await service.housekeep()
        async with service.lock:
            receipt = service.receipt("/experience/v1/answers", body)
            replay = await service.replay(receipt)
            if replay is not None:
                # Pre-history receipts remain valid without fabricating entries.
                replay.setdefault("inference_history", [])
                return replay
            if body["expected_revision"] != service.document["state"]["revision"]:
                raise DomainError("stale_experience", 409, "Experience changed. Refresh before answering.")
            doc = deepcopy(service.document)
            payload = self.payload(doc)
            payload["request"] = body
            result = await service.registry.dispatch("experience", "answer", payload)
            doc["meta"]["experience_answers"] = result["answers"]
            doc["state"]["revision"] += 1
            doc["state"]["generated_at"] = iso(service.clock())
            await self.capture_history(doc)
            result = await service.registry.dispatch("experience", "project", self.payload(doc))
            validate_experience("experience", result["experience"])
            receipt.update(response=result["experience"], preserve_response=True)
            await service.commit(doc, receipt, bump=False)
            return deepcopy(receipt["response"])
