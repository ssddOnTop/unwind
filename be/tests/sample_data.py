"""Test-only records and explicitly injected legacy prototype adapter."""
import json
from pathlib import Path
from companion.connections.sources import SourceConnection
from companion.contracts import DomainError
from copy import deepcopy
EXAMPLE = json.loads((Path(__file__).parent / "fixtures/state.example.json").read_text())
BATCH = json.loads((Path(__file__).parent / "fixtures/observation-batch.example.json").read_text())

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


def enable_test_fixtures(service):
    """Explicit test injection; no public request or deployment flag can enable it."""
    if service.allow_fixture_data:
        return service
    service.allow_fixture_data = True
    service.registry._extensions[("demo", "1")] = DemoExtension()
    evaluate = service.evaluate

    async def evaluate_with_test_adapter(doc):
        source = next(s for s in doc["state"]["sources"] if s["id"] == "demo")
        if source["capability"] == "not_configured":
            source.update(capability="available", status="ready", enabled=True)
            if "demo" not in doc["state"]["preferences"]["enabled_source_ids"]:
                doc["state"]["preferences"]["enabled_source_ids"].append("demo")
        await evaluate(doc)

    service.evaluate = evaluate_with_test_adapter
    return service


def build_service(*args, **kwargs):
    from companion.bootstrap import build_service as build
    replacements = dict(kwargs.pop("replacements", {}) or {})
    replacements.setdefault("demo", DemoExtension())
    service = build(*args, replacements=replacements, **kwargs)
    return enable_test_fixtures(service)


def create_app(*args, **kwargs):
    from companion.http import create_app as create
    replacements = dict(kwargs.pop("replacements", {}) or {})
    replacements.setdefault("demo", DemoExtension())
    app = create(*args, replacements=replacements, **kwargs)
    enable_test_fixtures(app.state.service)
    return app
