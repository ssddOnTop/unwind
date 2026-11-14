"""Local adapter boundary: query outcomes never grant application consent."""
from ..contracts import DomainError
from ..ingestion import validate_observation, validate_update


class SourceConnection:
    version = "1"

    def __init__(self, source_id, status="available"):
        self.id, self.status = source_id, status

    async def handle(self, operation: str, payload: dict) -> dict:
        if self.status != "available":
            raise DomainError("not_configured", 409)
        if operation == "validate_observation":
            validate_observation(payload["observation"], payload["evaluation_date"])
        elif operation == "validate_update":
            validate_update(payload)
        else:
            raise ValueError("Unsupported source operation")
        return {"valid": True}
