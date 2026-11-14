"""Public JSON domain interface, derived from the frozen schema (not a Swagger copy)."""
import json
from pathlib import Path
from typing import Annotated, Literal, Union

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict, Field, create_model

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
SCHEMA = json.loads((CONTRACTS / "state.schema.json").read_text())
RULES = json.loads((CONTRACTS / "scenarios.json").read_text())


class DomainError(Exception):
    def __init__(self, code="invalid_request", status=422, message="Request does not satisfy the v1 contract."):
        self.code, self.status, self.message = code, status, message
        super().__init__(message)


def validate(name: str, value: dict) -> None:
    schema = {"$ref": f"#/$defs/{name}", "$defs": SCHEMA["$defs"]}
    if not Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(value):
        code = "unsupported_version" if isinstance(value, dict) and value.get("schema_version", "1") != "1" else "invalid_request"
        raise DomainError(code)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SchemaModels:
    """Typed model factory for an explicit frozen schema profile."""
    def __init__(self, schema, prefix=""):
        self.schema, self.prefix, self.models = schema, prefix, {}



    def type(self, node, name):
        if "$ref" in node:
            return self.model(node["$ref"].split("/")[-1])
        if "const" in node:
            return Literal[node["const"]]
        if "enum" in node:
            return Literal[tuple(node["enum"])]
        for combinator in ("anyOf", "oneOf"):
            if combinator in node:
                return Union[tuple(self.type(n, name + str(i)) for i, n in enumerate(node[combinator]))]
        kind = node.get("type")
        if kind == "object":
            return self.object(node, name)
        if kind == "array":
            t = list[self.type(node["items"], name + "Item")]
            return Annotated[t, Field(max_length=node.get("maxItems"))]
        if kind == "null":
            return type(None)
        t = {"string": str, "integer": int, "boolean": bool}[kind]
        kwargs = {dest: node[src] for src, dest in [("minimum", "ge"), ("maximum", "le"),
                  ("minLength", "min_length"), ("maxLength", "max_length"), ("pattern", "pattern")] if src in node}
        if "format" in node:
            kwargs["json_schema_extra"] = {"format": node["format"]}
        return Annotated[t, Field(**kwargs)] if kwargs else t


    def inline_refs(self, node):
        # Conditional schema fragments have no model-generated reference names. Inline
        # their small leaf objects so generated OpenAPI keeps the normative allOf rules.
        if isinstance(node, list):
            return [self.inline_refs(v) for v in node]
        if isinstance(node, dict):
            if "$ref" in node:
                return self.inline_refs(self.schema["$defs"][node["$ref"].split("/")[-1]])
            return {k: self.inline_refs(v) for k, v in node.items()}
        return node


    def object(self, node, name):
        conditional = {k: self.inline_refs(node[k]) for k in ("allOf", "oneOf", "if", "then", "else") if k in node}
        return create_model(self.prefix + name, __base__=ContractModel,
                            __config__=ConfigDict(json_schema_extra=conditional),
                            **{k: (self.type(v, name + "_" + k), ...) for k, v in node["properties"].items()})


    def model(self, name):
        if name not in self.models:
            self.models[name] = self.object(self.schema["$defs"][name], name)
        return self.models[name]


BASE_MODELS = SchemaModels(SCHEMA)
model = BASE_MODELS.model

State = model("state")
Health = model("health")
ObservationBatch = model("observation_batch")
IngestionResponse = model("ingestion_response")
FeedbackRequest = model("feedback_request")
PreferencesRequest = model("preferences_request")
ProcessRequest = model("process_request")
DemoRequest = model("demo_request")
DeleteRequest = model("delete_request")
ErrorResponse = model("error")
