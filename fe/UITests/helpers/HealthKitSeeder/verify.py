#!/usr/bin/env python3
"""TEST ONLY. Verify private helper evidence: python3 verify.py EVIDENCE_DIR.
Reads only copied JSON evidence, never simulator Health databases or backend.
"""
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
report = json.loads((root / 'probe-result.json').read_text())
manifest = json.loads((root / 'owned-uuid-manifest.json').read_text())
rows = report['rows']
assert len(rows) == 5
assert len({row['uuid'] for row in rows}) == 5
assert {row['uuid'] for row in rows} == {row['uuid'] for row in manifest}
expected = {
    'HKQuantityTypeIdentifierStepCount': ('120.0', 'count', '2026-11-13T12:00:00Z', '2026-11-13T12:06:00Z'),
    'HKQuantityTypeIdentifierHeartRate': ('72.0', 'count/min', '2026-11-13T12:00:00Z', '2026-11-13T12:00:00Z'),
    'HKQuantityTypeIdentifierRestingHeartRate': ('60.0', 'count/min', '2026-11-13T08:00:00Z', '2026-11-13T08:00:00Z'),
}
for row in rows:
    assert row['ownedTypeCount'] == '1', row
    assert row['source'] == 'org.localcompanion.test.healthseeder'
    assert row['result'] == 'existing exact sync sample; no write'
    metadata = json.loads(row['metadataJSON'])
    # HealthKit round-trips Swift Bool through NSNumber; JSON may contain 1.
    assert metadata['org.localcompanion.test.healthseeder.synthetic'] in (True, 1)
    assert metadata['org.localcompanion.test.healthseeder.datasetID'] == report['datasetID']
    assert metadata['org.localcompanion.test.healthseeder.recipe'] == 'bounded-probe-v1'
    assert metadata['HKMetadataKeySyncVersion'] == 1
    assert metadata['HKMetadataKeySyncIdentifier'].startswith(report['datasetID'] + '.')
    if row['type'] in expected:
        assert tuple(row[k] for k in ('value', 'unit', 'start', 'end')) == expected[row['type']]
    elif row['type'] == 'HKCategoryTypeIdentifierSleepAnalysis':
        assert row['categoryValue'] == '3'
        assert (row['start'], row['end']) == ('2026-11-13T01:00:00Z', '2026-11-13T01:30:00Z')
    else:
        assert row['type'] == 'HKWorkoutTypeIdentifier'
        assert row['durationSeconds'] == '360.0'
        assert (row['start'], row['end']) == ('2026-11-13T13:00:00Z', '2026-11-13T13:06:00Z')
print('PASS: five exact UUID/source samples, values/intervals, provenance, one per type, idempotent rerun')
