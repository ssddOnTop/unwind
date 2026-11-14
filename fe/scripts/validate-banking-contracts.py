#!/usr/bin/env python3
"""Validate actual Swift-encoded banking actions offline with an existing jsonschema."""
from pathlib import Path
import json
import os
from jsonschema import Draft202012Validator, FormatChecker
root = Path(__file__).resolve().parents[2]
evidence = (root / 'fe/evidence' / os.environ.get('FE_EVIDENCE_SUBDIR', 'bank-integration')).resolve()
if not evidence.is_relative_to((root / 'fe/evidence').resolve()):
    raise SystemExit('Evidence must remain under fe/evidence')
schema = json.loads((root / 'contracts/banking.schema.json').read_text())
values = json.loads((evidence / 'encoded-banking-actions.json').read_text())
validator = Draft202012Validator({**schema, '$ref': '#/$defs/request'}, format_checker=FormatChecker())
for value in values:
    validator.validate(value)
inventory = json.loads((root / 'fe/Core/Tests/CompanionCoreTests/Fixtures/banking-inventory.example.json').read_text())
Draft202012Validator(schema, format_checker=FormatChecker()).validate(inventory)
report = {'swift_encoded_actions_validated': len(values), 'inventory_fixtures_validated': 1, 'format_validation': True}
(evidence / 'banking-schema-validation.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report))
