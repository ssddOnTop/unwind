#!/usr/bin/env python3
"""TEST ONLY. Verify copied HealthKit evidence against the immutable recipe.
Usage: python3 verify_full.py EVIDENCE_DIR [--expect-no-writes]
Requires canonical-recipe.json, full-result.json and full-owned-manifest.json.
Does not contact the simulator, HealthKit, backend or any database.
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path


def verify(directory, expect_no_writes=False):
    raw = (directory / 'canonical-recipe.json').read_bytes()
    recipe = json.loads(raw)
    result = json.loads((directory / 'full-result.json').read_text())
    manifest = json.loads((directory / 'full-owned-manifest.json').read_text())
    digest = hashlib.sha256(raw).hexdigest()
    assert result['phase'] == 'verified', result.get('error', result['phase'])
    assert result['sha256'] == manifest['sha256'] == digest
    assert result['sourceCommit'] == manifest['sourceCommit'] == recipe['source_commit']
    assert result['datasetID'] == manifest['datasetID'] == recipe['dataset_id']
    assert not result['representationDifferences']
    assert (result['start'], result['end']) == (recipe['start'], recipe['end'])
    rows = {row['sampleID']: row for row in result['rows']}
    records = {record['sample_id']: record for record in recipe['samples']}
    assert len(rows) == len(result['rows']) == len(records) == result['verified']
    assert set(rows) == set(records) == set(manifest['uuidsBySampleID'])
    assert len({row['uuid'] for row in rows.values()}) == len(rows)
    assert dict(Counter(row['type'] for row in rows.values())) == recipe['counts']
    assert dict(Counter(row['category'] for row in rows.values() if 'category' in row)) == recipe['category_counts']
    categories = {'inBed': 0, 'asleepCore': 3, 'asleepDeep': 4, 'asleepREM': 5, 'awake': 2}
    start = datetime.fromisoformat(recipe['start'].replace('Z', '+00:00'))
    end = datetime.fromisoformat(recipe['end'].replace('Z', '+00:00'))
    for sample_id, record in records.items():
        row = rows[sample_id]
        assert row['uuid'] == manifest['uuidsBySampleID'][sample_id]
        assert row['source'] == 'org.localcompanion.test.healthseeder'
        assert row['metadataVerified'] is True
        assert row['type'] == record['hk_type']
        assert (row['start'], row['end']) == (record['start_at'], record['end_at'])
        a, b = (datetime.fromisoformat(row[key].replace('Z', '+00:00')) for key in ('start', 'end'))
        assert start <= a < end and a <= b <= end
        if record['value'] is not None:
            assert (row['value'], row['unit']) == (record['value'], record['unit'])
        if record['category'] is not None:
            assert row['category'] == record['category']
            assert row['categoryValue'] == categories[record['category']]
        if record['hk_type'] == 'workout':
            for key in ('duration_seconds', 'energy_kcal', 'distance_m', 'workout_activity'):
                assert row[key] == record[key]
            assert row['duration_seconds'] == (b-a).total_seconds()
    assert result['written'] + result['reused'] == len(rows)
    if expect_no_writes:
        assert result['written'] == 0 and result['reused'] == len(rows)
    print(json.dumps({'verified': len(rows), 'counts': recipe['counts'], 'sha256': digest,
                      'written': result['written'], 'reused': result['reused']}, sort_keys=True))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--expect-no-writes', action='store_true')
    args = parser.parse_args()
    verify(args.directory, args.expect_no_writes)
