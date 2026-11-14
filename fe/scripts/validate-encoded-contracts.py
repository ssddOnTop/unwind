#!/usr/bin/env python3
"""Validate actual Swift-encoded synthetic payloads; uses an existing jsonschema installation."""
import json, os
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker
root=Path(__file__).resolve().parents[2]
out=(root/'fe/evidence'/os.environ.get('FE_EVIDENCE_SUBDIR', '')).resolve()
if not out.is_relative_to((root/'fe/evidence').resolve()): raise SystemExit('Evidence must remain under fe/evidence')
schema=json.loads((root/'contracts/state.schema.json').read_text())
cases=json.loads((out/'encoded-contracts.json').read_text())
results=[]
for case in cases:
    definition=case['definition']
    validator=Draft202012Validator({'$schema':schema['$schema'],'$ref':'#/$defs/'+definition,'$defs':schema['$defs']},format_checker=FormatChecker())
    errors=sorted(validator.iter_errors(case['payload']),key=lambda e:str(e.path))
    results.append({'definition':definition,'valid':not errors,'errors':[e.message for e in errors]})
report={'source':'actual Swift encoder output from CoreTests.exportEncodedContractCases','count':len(results),'passed':sum(r['valid'] for r in results),'results':results}
(out/'contract-validation.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
raise SystemExit(0 if all(r['valid'] for r in results) else 1)
