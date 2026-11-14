from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from ..contracts import DomainError
from ..domain import instant, iso, key, refkey


class FeedbackExtension:
    id, version, status = "feedback", "1", "available"

    async def handle(self, operation: str, payload: dict) -> dict:
        if operation != "apply":
            raise ValueError("Unsupported feedback operation")
        state, body = deepcopy(payload["state"]), payload["request"]
        card = next((c for c in state["ledger"] if c["id"] == body["card_id"]), None)
        if card is None:
            raise DomainError("not_found", 404)
        allowed = {"correctness": ({"correct", "incorrect", "partial"}, {"card", "event"}),
                   "usefulness": ({"useful", "not_useful", "partial"}, {"card"}),
                   "suppress": ({"stop"}, {"card", "scenario"})}
        values, scopes = allowed[body["kind"]]
        if body["value"] not in values or body["scope"] not in scopes:
            raise DomainError()
        if (body["scope"] == "event") != (body["event_ref"] is not None):
            raise DomainError()
        if body["event_ref"]:
            obs = next((o for o in state["observations"] if key(o) == refkey(body["event_ref"])), None)
            if obs is None or body["event_ref"] not in card["evidence"]:
                raise DomainError("not_found", 404)
            if body["attribution"] is not None and obs["kind"] != "purchase":
                raise DomainError()
        elif body["attribution"] is not None:
            raise DomainError()
        feedback = {k: deepcopy(body[k]) for k in ("card_id", "kind", "value", "scope", "event_ref", "attribution", "note")}
        created_at = instant(payload["created_at"])
        if state["feedback"]:
            created_at = max(created_at, instant(state["feedback"][-1]["created_at"]) + timedelta(microseconds=1))
        feedback.update(id=str(uuid4()), created_at=created_at.isoformat(timespec="microseconds").replace("+00:00", "Z"))
        state["feedback"].append(feedback)
        state["feedback"].sort(key=lambda f: (f["created_at"], f["id"]))
        if body["kind"] == "suppress" and card["scenario_id"] not in state["preferences"]["suppressed_scenario_ids"]:
            state["preferences"]["suppressed_scenario_ids"].append(card["scenario_id"])
        return {"state": state}
