"""Versioned capability registry and dispatcher; deliberately domain-free."""
from typing import Protocol


class Extension(Protocol):
    id: str
    version: str
    status: str

    async def handle(self, operation: str, payload: dict) -> dict: ...


class Registry:
    def __init__(self):
        self._extensions: dict[tuple[str, str], Extension] = {}

    def register(self, extension: Extension) -> None:
        key = (extension.id, extension.version)
        if key in self._extensions:
            raise ValueError(f"Duplicate capability/version: {key}")
        if extension.status not in {"available", "not_configured", "unsupported"}:
            raise ValueError("Invalid capability status")
        self._extensions[key] = extension

    def metadata(self) -> list[dict]:
        return [{"id": e.id, "version": e.version, "status": e.status}
                for e in self._extensions.values()]

    async def dispatch(self, capability: str, operation: str, payload: dict,
                       version: str = "1") -> dict:
        return await self._extensions[(capability, version)].handle(operation, payload)
