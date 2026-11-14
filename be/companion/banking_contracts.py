"""Strict additive banking v1 models; existing State is unchanged."""
import json
from jsonschema import Draft202012Validator, FormatChecker
from .contracts import CONTRACTS, DomainError, SchemaModels

SCHEMA=json.loads((CONTRACTS/'banking.schema.json').read_text())
MODELS=SchemaModels(SCHEMA,'banking_')
Banking=MODELS.model('banking')
# FastAPI/Pydantic builds the explicit discriminated request from these members.
REQUEST_MODELS={name.removesuffix('_request'):MODELS.model(name) for name in SCHEMA['$defs'] if name.endswith('_request')}
BankingError=MODELS.model('error')
from typing import Annotated,Union
from pydantic import Field
BankingRequest=Annotated[Union[tuple(REQUEST_MODELS.values())],Field(discriminator='action')]

def validate_banking(name,value):
    if not Draft202012Validator({'$ref':'#/$defs/'+name,'$defs':SCHEMA['$defs']},format_checker=FormatChecker()).is_valid(value):
        raise DomainError('unsupported_version' if isinstance(value,dict) and value.get('schema_version','1')!='1' else 'invalid_request')
