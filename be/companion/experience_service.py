"""Experience transaction adapter, scoped exclusively to the injected service instance."""
from copy import deepcopy
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
                "as_of_date": service.now(doc).astimezone(zone(state["preferences"]["timezone"])).date().isoformat(),
                "review_ready": bool(doc["meta"].get("experience_review_ready")) and not service.required_failure,
                "selected_sources": {"purchase_recurrence": "plaid" if doc["meta"].get("plaid_connected") else "demo",
                    "recorded_steps": "healthkit" if healthkit["enabled"] and healthkit["consent"] == "granted" else "demo"}}

    async def reconcile(self, doc):
        result = await self.service.registry.dispatch("experience", "clean", self.payload(doc))
        doc["meta"]["experience_answers"] = result["answers"]
        return result["removed"]

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
            result = await service.registry.dispatch("experience", "project", self.payload(doc))
            validate_experience("experience", result["experience"])
            receipt.update(response=result["experience"], preserve_response=True)
            await service.commit(doc, receipt, bump=False)
            return deepcopy(receipt["response"])
