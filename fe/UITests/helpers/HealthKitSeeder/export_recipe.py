#!/usr/bin/env python3
"""TEST ONLY. Export the selected generator's health output without importing its runtime.
Uses installed pydantic; installs nothing. Executes only exact AST definitions for
Strict/HealthSample/Transaction and six_month_start/generate plus DATASET/SOURCE.
No operator imports, env reads, DB connections, load(), main(), or transaction export.
Usage: python export_recipe.py --source-root REPO --end UTC_TIMESTAMP --output FILE
"""
import argparse
import ast
import calendar
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Literal
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, model_validator


def selected_definitions(raw, names, filename):
    tree = ast.parse(raw, filename=filename)
    selected = []
    found = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            selected.append(node); found.add(node.name)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names:
            ast.literal_eval(node.value)  # constants only; never execute side effects
            selected.append(node); found.add(node.targets[0].id)
    if found != set(names):
        raise ValueError(f'Missing definitions: {set(names) - found}')
    return compile(ast.Module(body=selected, type_ignores=[]), filename, 'exec')


def export(source_root, end, output):
    source_root = source_root.resolve()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source_root), *args], text=True).strip()
    commit = git('rev-parse', 'HEAD')
    paths = ['be/companion/pattern_operator.py', 'be/companion/patterns.py']
    originals = {p: (source_root / p).read_bytes() for p in paths}
    if git('status', '--porcelain', '--', *paths):
        raise ValueError('Recipe source must be committed before export')
    namespace = dict(calendar=calendar, datetime=datetime, timedelta=timedelta,
                     timezone=timezone, ZoneInfo=ZoneInfo, Literal=Literal,
                     BaseModel=BaseModel, ConfigDict=ConfigDict, Field=Field,
                     model_validator=model_validator, __name__='isolated_recipe')
    exec(selected_definitions(originals[paths[1]], {'Strict', 'HealthSample', 'Transaction'}, paths[1]), namespace)
    exec(selected_definitions(originals[paths[0]], {'DATASET', 'SOURCE', 'six_month_start', 'generate'}, paths[0]), namespace)
    end = datetime.fromisoformat(end.replace('Z', '+00:00'))
    if end.tzinfo is None or end.utcoffset() != timedelta(0) or end.time().isoformat() != '00:00:00':
        raise ValueError('End must be UTC midnight')
    start, samples, _ = namespace['generate'](end)
    ids = set()
    asleep = []
    for sample in samples:
        namespace['HealthSample'].model_validate(sample)
        a, b = sample['start_at'], sample['end_at']
        if not start <= a < end or not a <= b <= end:
            raise ValueError(f'Out-of-window sample: {sample["sample_id"]}')
        if sample['hk_type'] not in ('heartRate', 'restingHeartRate') and a == b:
            raise ValueError('Zero-length interval')
        if sample['sample_id'] in ids:
            raise ValueError('Duplicate canonical sample ID')
        ids.add(sample['sample_id'])
        if sample['hk_type'] == 'workout' and sample['duration_seconds'] != (b-a).total_seconds():
            raise ValueError('Workout duration mismatch')
        if sample['category'] in ('asleepCore', 'asleepDeep', 'asleepREM'):
            asleep.append((a, b))
    asleep.sort()
    if any(a[1] > b[0] for a, b in zip(asleep, asleep[1:])):
        raise ValueError('Overlapping asleep stages')
    if commit != git('rev-parse', 'HEAD') or any((source_root/p).read_bytes() != raw for p, raw in originals.items()):
        raise ValueError('Source changed during export')
    def serialize(value):
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        raise TypeError(type(value).__name__)
    document = {'schema_version': 1, 'dataset_id': namespace['DATASET'], 'recipe_version': 1,
                'source_commit': commit,
                'source_sha256': {p: hashlib.sha256(raw).hexdigest() for p, raw in originals.items()},
                'start': start, 'end': end, 'timezone': 'UTC',
                'counts': dict(Counter(s['hk_type'] for s in samples)),
                'category_counts': dict(Counter(s['category'] for s in samples if s['category'])),
                'samples': sorted(samples, key=lambda s: s['sample_id'])}
    raw = json.dumps(document, default=serialize, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    digest = hashlib.sha256(raw).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    output.with_suffix('.sha256').write_text(digest + '\n')
    print(json.dumps({'sha256': digest, 'source_commit': commit, 'count': len(samples),
                      'counts': document['counts'], 'category_counts': document['category_counts'],
                      'start': serialize(start), 'end': serialize(end)}, sort_keys=True))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    export(args.source_root, args.end, args.output)
