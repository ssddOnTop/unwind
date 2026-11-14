#!/usr/bin/env python3
"""Validate actual Swift-encoded experience payloads using an already-installed jsonschema."""
from pathlib import Path
import json
import os
from jsonschema import Draft202012Validator, FormatChecker
root = Path(__file__).resolve().parents[2]
evidence = (root / 'fe/evidence' / os.environ.get('FE_EVIDENCE_SUBDIR', 'auth')).resolve()
if not evidence.is_relative_to((root / 'fe/evidence').resolve()):
    raise SystemExit('Evidence must remain under fe/evidence')
schema = json.loads((root / 'contracts/experience.schema.json').read_text())
cases = json.loads((evidence / 'encoded-experience-contracts.json').read_text())
for case in cases:
    selected = {**schema, '$ref': '#/$defs/' + case['definition']}
    Draft202012Validator(selected, format_checker=FormatChecker()).validate(case['payload'])
report = {'cases_validated': len(cases), 'schema': 'contracts/experience.schema.json', 'format_validation': True}
(evidence / 'experience-schema-validation.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report))
