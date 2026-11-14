from copy import deepcopy

from ..connections.sources import SourceConnection
from ..contracts import DomainError, EXAMPLE


class DemoExtension(SourceConnection):
    def __init__(self):
        super().__init__("demo")

    async def handle(self, operation: str, payload: dict) -> dict:
        if operation not in {"seed", "reset"}:
            return await super().handle(operation, payload)
        state = payload["state"]
        # Historical consent is deliberately remembered even after revocation.
        if any(o["mode"] == "live" or o["source_id"] != "demo" for o in state["observations"]) or any(
            s["id"] != "demo" and (s["enabled"] or s["consent"] != "not_requested") for s in state["sources"]):
            raise DomainError("demo_conflict", 409)
        if operation == "seed" and state["observations"]:
            if state["observations"] != EXAMPLE["observations"]:
                raise DomainError("demo_conflict", 409)
            return {"state": state, "changed": False}
        if operation == "seed" and (state["feedback"] or payload.get("fixture_session")):
            raise DomainError("demo_conflict", 409)
        fixture = deepcopy(EXAMPLE)
        fixture["data_epoch"] = state["data_epoch"]
        return {"state": fixture, "changed": True}
