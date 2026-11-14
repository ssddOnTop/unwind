#!/usr/bin/env python3
"""Offline JSON Schema checks of synthetic fixtures and actual Swift encoder output.
Requires the existing jsonschema/referencing installation; never installs dependencies.
"""
import json, os
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

root = Path(__file__).resolve().parents[2]
out = (root/'fe/evidence'/os.environ.get('FE_EVIDENCE_SUBDIR', 'plaid')).resolve()
if not out.is_relative_to((root/'fe/evidence').resolve()): raise SystemExit('Evidence must remain under fe/evidence')
base_path = root/'contracts/state.schema.json'
profile_path = root/'contracts/plaid.schema.json'
base = json.loads(base_path.read_text())
profile = json.loads(profile_path.read_text())
registry = Registry().with_resources([
    (base_path.as_uri(), Resource.from_contents(base)),
    (profile_path.as_uri(), Resource.from_contents(profile)),
])
cases = json.loads((out/'encoded-plaid-contracts.json').read_text())
results = []
for case in cases:
    schema = {'$schema': profile['$schema'], '$ref': profile_path.as_uri()+'#/$defs/'+case['definition']}
    validator = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
    errors = list(validator.iter_errors(case['payload']))
    results.append({'definition': case['definition'], 'valid': not errors,
                    'errors': [{'path': list(e.absolute_path), 'message': e.message} for e in errors]})
report = {'source': 'Swift-encoded frozen synthetic examples plus a count-only unknown-attribution Sandbox fixture',
          'count': len(results), 'passed': sum(r['valid'] for r in results), 'results': results}
(out/('plaid-contract-validation.json' if os.environ.get('FE_EVIDENCE_SUBDIR') else 'contract-validation.json')).write_text(json.dumps(report, indent=2)+'\n')
print(json.dumps(report, indent=2))
raise SystemExit(0 if all(r['valid'] for r in results) else 1)
