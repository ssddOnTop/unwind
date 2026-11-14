"""Explicit opt-in Sandbox profile; frozen base files are never changed."""
from copy import deepcopy
import json
from typing import Annotated, Union

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import Field

from .contracts import CONTRACTS, DomainError, SCHEMA, SchemaModels


def local_refs(node):
    if isinstance(node, list):
        return [local_refs(v) for v in node]
    if isinstance(node, dict):
        return {k: v.replace("state.schema.json#", "#") if k == "$ref" else local_refs(v) for k, v in node.items()}
    return node


PROFILE = deepcopy(SCHEMA)
PROFILE["$defs"].update(local_refs(json.loads((CONTRACTS / "plaid.schema.json").read_text())["$defs"]))
MODELS = SchemaModels(PROFILE, "plaid_")
PlaidState = MODELS.model("state")
PlaidIngestionResponse = MODELS.model("ingestion_response")
PlaidRequest = Annotated[Union[tuple(MODELS.model(n + "_request") for n in ("link_token", "exchange", "sync", "disconnect"))], Field(discriminator="action")]
PlaidResponse = Annotated[Union[tuple(MODELS.model(n + "_response") for n in ("link_token", "exchange", "sync", "disconnect"))], Field(discriminator="action")]


def validate_profile(name, value):
    schema = {"$ref": f"#/$defs/{name}", "$defs": PROFILE["$defs"]}
    if not Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(value):
        raise DomainError("unsupported_version" if isinstance(value, dict) and value.get("schema_version", "1") != "1" else "invalid_request")
