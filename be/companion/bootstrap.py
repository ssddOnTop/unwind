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


def build_service(data_dir: Path, replacements=None, scheduler=None, clock=None, synthesis_timeout=30, plaid_profile=False, experience_profile=False, webhook_url=None, account_id=None, plaid_owner_check=None, banking_profile=False, banking_claim_item=None):
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
    if plaid_profile:
        from .connections.plaid import PlaidSandboxClient, SandboxCredentials
        from .extensions.plaid import PlaidExtension
        implementations["plaid"] = lambda: PlaidExtension(data_dir, PlaidSandboxClient(SandboxCredentials.from_environment()), webhook_url=webhook_url, client_user_id=account_id, ownership_check=plaid_owner_check)
    if banking_profile:
        from .extensions.banking import BankingExtension
        from .connections.plaid import PlaidSandboxClient, SandboxCredentials
        implementations['banking'] = lambda: BankingExtension(data_dir, PlaidSandboxClient(SandboxCredentials.from_environment()), webhook_url=webhook_url, client_user_id=account_id, ownership_check=plaid_owner_check, claim_item=banking_claim_item)
    if experience_profile:
        from .extensions.experience import ExperienceExtension
        implementations["experience"] = ExperienceExtension
    for capability, factory in implementations.items():
        extension = replacements.get(capability)
        registry.register(extension if extension is not None else factory())
    kwargs = {"clock": clock} if clock else {}
    service = CompanionService(registry, scheduler or NotificationScheduler(), synthesis_timeout=synthesis_timeout, **kwargs)
    if plaid_profile:
        from .plaid_service import PlaidCoordinator
        from .plaid_contracts import validate_profile
        service.plaid_coordinator = PlaidCoordinator(service)
        service.state_validator = lambda s: validate_profile("state", s)
    if banking_profile:
        from .banking_service import BankingCoordinator
        service.banking_coordinator = BankingCoordinator(service)
        service.plaid_coordinator = service.banking_coordinator
    if experience_profile:
        from .experience_service import ExperienceCoordinator
        service.experience_coordinator = ExperienceCoordinator(service)
    return service
