"""Composition root. This is the only place that selects extension implementations."""
from pathlib import Path

from .connections.sources import SourceConnection
from .core import Registry
from .domain import synthesis
from .extensions.demo import DemoExtension
from .extensions.feedback import FeedbackExtension
from .extensions.persistence import PersistenceExtension
from .extensions.scenarios import ScenariosExtension
from .scheduling import NotificationScheduler
from .service import CompanionService


class UnavailableSynthesis:
    id, version, status = "synthesis", "1", "not_configured"

    def __init__(self, checkpoint_path: Path | None = None):
        self.checkpoint_path = checkpoint_path

    async def handle(self, operation: str, payload: dict) -> dict:
        if operation == "clear":
            if self.checkpoint_path is not None:
                for suffix in ("", "-wal", "-shm", "-journal"):
                    self.checkpoint_path.with_name(self.checkpoint_path.name + suffix).unlink(missing_ok=True)
            return {"cleared": True}
        if operation != "synthesize":
            raise ValueError("Unsupported synthesis operation")
        mode = payload["mode"]
        if mode == "disabled":
            return synthesis(mode, "skipped", "disabled")
        if mode == "live":
            return synthesis(mode, "skipped", "no_credentials")
        return synthesis(mode, "failed", "internal_error")


def build_service(data_dir: Path, replacements=None, scheduler=None, clock=None, synthesis_timeout=30):
    replacements = replacements or {}
    registry = Registry()
    implementations = {
        "persistence": lambda: PersistenceExtension(data_dir / "state.sqlite"),
        "scenarios": ScenariosExtension, "feedback": FeedbackExtension, "demo": DemoExtension,
        "healthkit": lambda: SourceConnection("healthkit"),
        "plaid": lambda: SourceConnection("plaid", "not_configured"),
        "email": lambda: SourceConnection("email", "not_configured"),
    }
    def synthesis_factory():
        try:
            from .extensions.synthesis import SynthesisExtension
        except ImportError:
            return UnavailableSynthesis(data_dir / "checkpoints.sqlite")
        return SynthesisExtension(data_dir / "checkpoints.sqlite")
    implementations["synthesis"] = synthesis_factory
    for capability, factory in implementations.items():
        extension = replacements.get(capability)
        registry.register(extension if extension is not None else factory())
    kwargs = {"clock": clock} if clock else {}
    return CompanionService(registry, scheduler or NotificationScheduler(), synthesis_timeout=synthesis_timeout, **kwargs)
