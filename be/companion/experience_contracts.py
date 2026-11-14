"""Typed, versioned experience profile; never widens the frozen State schema."""
import json
from jsonschema import Draft202012Validator, FormatChecker
from .contracts import CONTRACTS, DomainError, SchemaModels

EXPERIENCE_SCHEMA = json.loads((CONTRACTS / "experience.schema.json").read_text())
MODELS = SchemaModels(EXPERIENCE_SCHEMA, "experience_")
Experience = MODELS.model("experience")
AnswerRequest = MODELS.model("answer_request")
ExperienceError = MODELS.model("error")


def validate_experience(name, value):
    schema = {"$ref": f"#/$defs/{name}", "$defs": EXPERIENCE_SCHEMA["$defs"]}
    if not Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(value):
        raise DomainError("unsupported_version" if isinstance(value, dict) and value.get("schema_version", "1") != "1" else "invalid_request")
