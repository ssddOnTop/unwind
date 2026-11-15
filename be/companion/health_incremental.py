"""Explicit Health deltas on the existing account persistence transaction.

Never call snapshot stage_health with these pages. Missing UUIDs never imply deletion.
The route wrapper uses PatternCoordinator's existing persistence connection/lock; it
neither opens an alternate database nor invokes a provider or background scheduler.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Annotated, Literal
from uuid import UUID, uuid5, NAMESPACE_URL
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, model_validator
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from .contracts import DomainError
from .health_ingestion import HealthRecord, HELPER, DATASET, RECIPE
from .patterns import Strict

ROUTE = '/health/v1/incremental'
UTC = timezone.utc
DAY = timedelta(days=1)
MAX_OBJECTS = 50_000
MAX_BODY_BYTES = 262_144
DAILY_BUNDLE = 'healthkit.daily-source-selection'
HKType = Literal['stepCount', 'sleepAnalysis', 'heartRate', 'restingHeartRate', 'workout']
UUID_PATTERN = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def stable(value):
    return hashlib.sha256(value.encode()).hexdigest()


def classify(sample):
    """Exact HealthNormalization.provenance rule; reuse backend helper constants.

    The native adapter must compute synthetic with HealthNormalization.syntheticFlag.
    Unknown synthetic metadata, mislabeled helpers and bounded probes are excluded,
    never silently relabeled as real. Validated HealthRecord rechecks provenance too.
    """
    if sample.source_bundle == HELPER:
        return 'synthetic' if (sample.synthetic and sample.dataset_id == DATASET
            and sample.recipe == RECIPE and sample.metadata_timezone == 'UTC') else None
    return 'realHealthKit' if not sample.synthetic and sample.dataset_id is None and sample.recipe is None else None


def admitted_provenance(sample):
    # Explicitly acknowledge unsupported subtypes without inventing normalized records.
    # The exact provenance classifier remains separate and unchanged.
    return classify(sample) if sample.excluded_reason is None else None


class IncrementalSample(Strict):
    hk_uuid: str = Field(pattern=UUID_PATTERN)
    hk_type: HKType
    start_at: datetime = Field(strict=False)
    end_at: datetime = Field(strict=False)
    timezone: str = Field(min_length=1, max_length=80)
    value: float | None = Field(ge=0, le=10_000_000, allow_inf_nan=False)
    unit: Literal['count', 'bpm'] | None
    category: Literal['inBed', 'asleepCore', 'asleepDeep', 'asleepREM', 'awake', 'asleepUnspecified'] | None
    workout_activity: Literal['walking', 'running', 'cycling'] | None
    duration_seconds: float | None = Field(ge=0, le=604800, allow_inf_nan=False)
    energy_kcal: float | None = Field(ge=0, le=10_000_000, allow_inf_nan=False)
    distance_m: float | None = Field(ge=0, le=10_000_000, allow_inf_nan=False)
    source_name: str = Field(min_length=1, max_length=80)
    source_bundle: str = Field(min_length=1, max_length=180, pattern=r'^[A-Za-z0-9.-]+$')
    source_revision: str = Field(min_length=1, max_length=80)
    # Hash the existing bundle/device stream identity locally; never send device IDs.
    source_stream: str | None = Field(pattern=r'^[a-f0-9]{64}$')
    synthetic: bool
    dataset_id: str | None = Field(max_length=160)
    recipe: str | None = Field(max_length=160)
    metadata_timezone: str | None = Field(max_length=80)
    excluded_reason: Literal['unsupported_workout_activity', 'unsupported_sleep_category', 'unsupported_step_source'] | None

    @model_validator(mode='after')
    def semantics(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Recognized timezone required') from None
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None or not timedelta(0) <= self.end_at-self.start_at <= timedelta(days=7):
            raise ValueError('Ordered timezone-aware interval of at most seven days required')
        self.hk_uuid = str(UUID(self.hk_uuid)).upper()
        if self.excluded_reason is not None:
            expected = {'unsupported_workout_activity': ('workout', self.workout_activity),
                        'unsupported_sleep_category': ('sleepAnalysis', self.category),
                        'unsupported_step_source': ('stepCount', self.source_stream)}[self.excluded_reason]
            if self.hk_type != expected[0] or expected[1] is not None:
                raise ValueError('Exclusion must name an actually unmapped subtype')
        provenance = admitted_provenance(self)
        if self.hk_type == 'stepCount' and provenance == 'realHealthKit':
            if self.unit != 'count' or self.value is None or self.source_stream is None:
                raise ValueError('Raw step contribution requires count and hashed source stream')
            if any(v is not None for v in (self.category, self.workout_activity, self.duration_seconds, self.energy_kcal, self.distance_m)):
                raise ValueError('Unexpected step fields')
        elif provenance is not None:
            if self.source_stream is not None:
                raise ValueError('Source stream is only needed for real step contributions')
            normalized(self)  # Reuse existing normalized semantic/provenance validation.
        return self


def normalized(sample):
    provenance = classify(sample)
    kind = {'stepCount': 'dailyCumulative', 'sleepAnalysis': 'category', 'heartRate': 'quantity',
            'restingHeartRate': 'quantity', 'workout': 'workout'}[sample.hk_type]
    return HealthRecord(sample_id=stable(sample.source_bundle + ':' + sample.hk_uuid), hk_type=sample.hk_type,
        kind=kind, start_at=sample.start_at, end_at=sample.end_at,
        timezone='UTC' if provenance == 'synthetic' else sample.timezone,
        value=sample.value, unit=sample.unit, category=sample.category, workout_activity=sample.workout_activity,
        duration_seconds=sample.duration_seconds, energy_kcal=sample.energy_kcal, distance_m=sample.distance_m,
        source_name='Synthetic HealthKit helper' if provenance == 'synthetic' else sample.source_name,
        revision=1, provenance=provenance, source_bundle=sample.source_bundle,
        source_revision=sample.source_revision, dataset_id=sample.dataset_id, recipe=sample.recipe)


class HealthIncrementalRequest(Strict):
    schema_version: Literal['1']
    request_id: str = Field(pattern=UUID_PATTERN)
    data_epoch: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$')
    hk_type: HKType
    stream_id: str = Field(pattern=UUID_PATTERN)
    sequence: int = Field(ge=0, le=2_147_483_647)
    # True when the next anchored page must be drained. Initial step projection waits
    # for false, so partial baseline chunks are not represented as full daily totals.
    has_more: bool
    samples: list[IncrementalSample] = Field(max_length=100)
    deleted_uuids: list[Annotated[str, Field(pattern=UUID_PATTERN)]] = Field(max_length=100)

    @model_validator(mode='after')
    def page(self):
        if len(self.samples) + len(self.deleted_uuids) > 100:
            raise ValueError('At most 100 additions plus deletions per page')
        if self.has_more and not (self.samples or self.deleted_uuids):
            raise ValueError('An empty page cannot have more results')
        for value in self.deleted_uuids:
            if len(value) != 36 or str(UUID(value)).lower() != value.lower():
                raise ValueError('Canonical deleted UUID required')
        self.deleted_uuids = [str(UUID(value)).upper() for value in self.deleted_uuids]
        ids = [sample.hk_uuid for sample in self.samples]
        if len(set(ids)) != len(ids) or len(set(self.deleted_uuids)) != len(self.deleted_uuids):
            raise ValueError('Repeated UUIDs within additions or deletions')
        # If HealthKit delivers addition and deletion together, deletion wins below.
        if any(s.hk_type != self.hk_type for s in self.samples):
            raise ValueError('One HealthKit type per anchored stream')
        return self


class HealthIncrementalReceipt(Strict):
    schema_version: Literal['1']
    request_id: str = Field(pattern=UUID_PATTERN)
    data_epoch: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$')
    stream_id: str = Field(pattern=UUID_PATTERN)
    sequence: int = Field(ge=0, le=2_147_483_647)
    hk_type: HKType
    upserted: int = Field(ge=0, le=100)
    deleted: int = Field(ge=0, le=100)
    excluded: int = Field(ge=0, le=100)
    expired: int = Field(ge=0, le=100)
    superseded: int = Field(ge=0, le=100)
    unknown_deletions: int = Field(ge=0, le=100)
    bootstrapped: bool


def parse_request(body):
    try:
        wire = json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(wire.encode()) > MAX_BODY_BYTES:
            raise DomainError('invalid_request', 422, 'Health delta exceeds the bounded page size.')
        return HealthIncrementalRequest.model_validate_json(wire), stable(wire)
    except (TypeError, ValueError, ValidationError):
        raise DomainError('invalid_request',422,'Health delta does not satisfy the incremental contract.') from None


def batch_identity(account, provenance, bundle):
    dataset = 'healthkit-' + stable(provenance + ':' + bundle)[:40]
    return str(uuid5(NAMESPACE_URL, account + ':' + dataset)), dataset


async def upsert_normalized(conn, account, record):
    batch, dataset = batch_identity(account, record.provenance, record.source_bundle)
    await conn.execute('''INSERT INTO companion_private.pattern_batches
        (account_id,batch_id,dataset,provenance,start_at,end_at,origin,active)
        VALUES(%s,%s,%s,%s,%s,%s,'healthkit',true) ON CONFLICT(account_id,batch_id)
        DO UPDATE SET start_at=LEAST(companion_private.pattern_batches.start_at,excluded.start_at),
        end_at=GREATEST(companion_private.pattern_batches.end_at,excluded.end_at),active=true''',
        (account,batch,dataset,record.provenance,record.start_at,record.end_at))
    columns = ('sample_id','hk_type','kind','start_at','end_at','timezone','value','unit','category',
               'workout_activity','duration_seconds','energy_kcal','distance_m','source_name','source_bundle','source_revision')
    semantic = columns[1:]
    changed = 'companion_private.health_samples.deleted OR (' + ','.join('companion_private.health_samples.'+k for k in semantic) + ') IS DISTINCT FROM (' + ','.join('excluded.'+k for k in semantic) + ')'
    await conn.execute('INSERT INTO companion_private.health_samples(account_id,batch_id,'+','.join(columns)+') VALUES('+','.join(['%s']*(len(columns)+2))+') ON CONFLICT(account_id,batch_id,sample_id) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in semantic)+',deleted=false,revision=companion_private.health_samples.revision+1 WHERE '+changed,
        (account,batch,*(getattr(record,k) for k in columns)))
    return batch, record.sample_id


def contribution_days(sample):
    day = sample.start_at.astimezone(UTC).replace(hour=0,minute=0,second=0,microsecond=0)
    last = sample.end_at.astimezone(UTC)
    # A zero-duration point belongs only to its start day; midnight end is exclusive.
    while day < last or day == sample.start_at.astimezone(UTC).replace(hour=0,minute=0,second=0,microsecond=0):
        yield day
        day += DAY


def daily_total(samples, day, as_of=None):
    totals = defaultdict(float)
    for sample in samples:
        duration = (sample.end_at - sample.start_at).total_seconds()
        limit = min(day+DAY, as_of) if as_of is not None else day+DAY
        overlap = max(0, (min(limit, sample.end_at) - max(day, sample.start_at)).total_seconds())
        part = sample.value * overlap / duration if duration > 0 else (sample.value if day <= sample.start_at < limit else 0)
        if part > 0 or (duration == 0 and day <= sample.start_at < limit):
            totals[(sample.source_bundle,sample.source_stream)] += part
    # Same conservative rule as the native foreground adapter: sum interval contributions
    # within each device/source stream, then choose the maximum, never sum phone + watch.
    value = max(totals.values()) if totals else None
    if value is not None and (not math.isfinite(value) or value > 10_000_000):
        raise DomainError('invalid_request',422,'Step total exceeds supported bounds.')
    return value


async def recompute_day(conn, account, epoch, day, now):
    cutoff = (now.astimezone(UTC)-timedelta(days=190)).replace(hour=0,minute=0,second=0,microsecond=0)
    if day < cutoff:
        return  # Explicit retention prevents late deletions recreating expired projections.
    rows = await (await conn.execute('''SELECT raw FROM companion_private.health_incremental_objects
        WHERE account_id=%s AND epoch=%s AND hk_type='stepCount' AND NOT deleted AND NOT excluded
        AND start_at<%s AND end_at>=%s''', (account,epoch,day+DAY,day))).fetchall()
    samples = [IncrementalSample.model_validate_json(json.dumps(row[0])) for row in rows]
    samples = [s for s in samples if classify(s) == 'realHealthKit']
    value = daily_total(samples, day, as_of=now)
    batch, _ = batch_identity(account, 'realHealthKit', DAILY_BUNDLE)
    sample_id = stable('daily-steps:' + stamp(day))
    if value is None:
        # Only an explicitly affected day is visited. This is NOT snapshot absence deletion.
        await conn.execute('''UPDATE companion_private.health_samples SET deleted=true,revision=revision+1
            WHERE account_id=%s AND batch_id=%s AND sample_id=%s AND NOT deleted''', (account,batch,sample_id))
    else:
        record = HealthRecord(sample_id=sample_id,hk_type='stepCount',kind='dailyCumulative',start_at=day,
            end_at=min(day+DAY,now),timezone='UTC',value=value,unit='count',source_name='HealthKit daily maximum source total (not combined devices)',
            revision=1,provenance='realHealthKit',source_bundle=DAILY_BUNDLE,source_revision='daily-max-source-v1',dataset_id=None,recipe=None)
        await upsert_normalized(conn,account,record)
    await conn.execute('INSERT INTO companion_private.health_incremental_days(account_id,epoch,day) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (account,epoch,day.date()))


async def prune_health_incremental_history(conn, account, epoch, *, now):
    """Caller-owned transaction; also callable by existing account housekeeping.

    Retire expired raw material on a 190-day UTC boundary, preserving stream sequence
    fences. This is explicit retention, never an inference from an empty Health query.
    The owner must call this from housekeeping even when Health collection is disabled.
    """
    account = str(UUID(account))
    if now.tzinfo is None or conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError('Protected account transaction and aware clock required')
    scope = await (await conn.execute("SELECT current_setting('companion.account_id',true)")).fetchone()
    if scope[0] != account:
        raise DomainError('invalid_request',403,'Account scope mismatch.')
    await conn.execute('SELECT generation FROM companion_private.accounts WHERE account_id=%s FOR NO KEY UPDATE',(account,))
    row = await (await conn.execute('SELECT body FROM companion_private.snapshots WHERE account_id=%s FOR UPDATE',(account,))).fetchone()
    if row is None or row[0]['state']['data_epoch'] != epoch:
        raise DomainError('epoch_mismatch',409)
    cutoff = (now.astimezone(UTC)-timedelta(days=190)).replace(hour=0,minute=0,second=0,microsecond=0)
    await conn.execute('''DELETE FROM companion_private.health_samples h USING companion_private.health_incremental_objects o
        WHERE o.account_id=%s AND o.epoch=%s AND h.end_at<%s AND h.account_id=o.account_id
        AND h.batch_id=o.batch_id AND h.sample_id=o.sample_id''',(account,epoch,cutoff))
    await conn.execute('''DELETE FROM companion_private.health_samples h USING companion_private.health_incremental_days d
        WHERE d.account_id=%s AND d.epoch=%s AND d.day<%s AND h.account_id=d.account_id
        AND h.source_bundle=%s AND h.hk_type='stepCount' AND h.start_at=(d.day::timestamp AT TIME ZONE 'UTC')''',
        (account,epoch,cutoff.date(),DAILY_BUNDLE))
    await conn.execute('''DELETE FROM companion_private.health_incremental_objects
        WHERE account_id=%s AND epoch=%s AND (end_at<%s OR (raw IS NULL AND updated_at<%s))''',(account,epoch,cutoff,cutoff))
    await conn.execute('DELETE FROM companion_private.health_incremental_days WHERE account_id=%s AND epoch=%s AND day<%s',(account,epoch,cutoff.date()))


async def stage_health_incremental(conn, account, body, *, now):
    """Apply a page + payload-bound receipt in caller's transaction; never commit here.

    Caller owns the authenticated persistence session and sets transaction-local account
    scope. We verify it even on a privileged connection, then lock in the same order as
    existing persistence: account, snapshot. Epoch and consent checks precede replay.
    """
    page, digest = parse_request(body)
    account = str(UUID(account))
    if now.tzinfo is None:
        raise ValueError('Timezone-aware clock required')
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError('Health ingestion requires an explicit transaction')
    scope = await (await conn.execute("SELECT current_setting('companion.account_id',true)")).fetchone()
    if scope[0] != account:
        raise DomainError('invalid_request',403,'Account scope mismatch.')
    locked = await (await conn.execute('SELECT generation FROM companion_private.accounts WHERE account_id=%s FOR NO KEY UPDATE',(account,))).fetchone()
    row = await (await conn.execute('SELECT body FROM companion_private.snapshots WHERE account_id=%s FOR UPDATE',(account,))).fetchone()
    if locked is None or row is None:
        raise DomainError('not_configured',409)
    state = row[0]['state']
    if state['data_epoch'] != page.data_epoch:
        raise DomainError('epoch_mismatch',409)
    source = next((s for s in state['sources'] if s['id']=='healthkit'),None)
    if not source or not source['enabled'] or source['consent'] != 'granted':
        raise DomainError('consent_required',409)
    # Replay proof survives generic receipt/history retention for this entire epoch.
    # It contains only a payload digest and bounded receipt, never raw Health records.
    prior = await (await conn.execute('SELECT digest,response FROM companion_private.health_incremental_acknowledgments WHERE account_id=%s AND epoch=%s AND request_id=%s', (account,page.data_epoch,page.request_id))).fetchone()
    if prior:
        if prior[0] != digest or prior[1] is None:
            raise DomainError('idempotency_conflict',409)
        return deepcopy(prior[1])
    cutoff = (now.astimezone(UTC)-timedelta(days=190)).replace(hour=0,minute=0,second=0,microsecond=0)
    current_samples = []
    for sample in page.samples:
        # Validate future timestamps even on an otherwise expired interval. Age alone
        # must not poison an anchored page or prevent its valid additions/deletions.
        if sample.end_at > now+timedelta(minutes=5) or sample.start_at >= now:
            raise DomainError('invalid_request',422,'Health sample has an invalid future timestamp.')
        # Retention is end-based, like the existing history pruning. An interval
        # overlapping the retained window is not expired just because it starts earlier.
        if sample.end_at >= cutoff:
            current_samples.append(sample)
    await prune_health_incremental_history(conn,account,page.data_epoch,now=now)
    stream = await (await conn.execute('SELECT stream_id,next_sequence,bootstrapped FROM companion_private.health_incremental_streams WHERE account_id=%s AND epoch=%s AND hk_type=%s FOR UPDATE',(account,page.data_epoch,page.hk_type))).fetchone()
    if (stream and (str(stream[0]) != str(UUID(page.stream_id)) or stream[1] != page.sequence)) or (not stream and page.sequence != 0):
        raise DomainError('invalid_request',409,'Health stream or sequence mismatch; replay the original pending page.')
    if stream is None:
        pending_snapshot = await (await conn.execute('SELECT 1 FROM companion_private.health_import_pages WHERE account_id=%s LIMIT 1',(account,))).fetchone()
        if pending_snapshot:
            raise DomainError('invalid_request',409,'Finish or explicitly discard the pending complete-window Health import before starting an incremental stream.')
    ready = bool(stream and stream[2])
    if not ready and (page.sequence > 500 or (page.sequence == 500 and (page.samples or page.deleted_uuids or page.has_more))):
        raise DomainError('invalid_request',422,'Initial Health collection exceeds the supported page bound.')
    if page.hk_type == 'stepCount' and not ready:
        # Legacy complete daily totals lack underlying UUID/device contributions. Do not
        # overwrite them with a partial new baseline or guess which day an old UUID deleted.
        legacy = await (await conn.execute('''SELECT 1 FROM companion_private.health_samples h
            JOIN companion_private.pattern_batches b USING(account_id,batch_id)
            WHERE h.account_id=%s AND h.hk_type='stepCount' AND NOT h.deleted
            AND b.provenance='realHealthKit' AND b.origin='healthkit'
            AND NOT EXISTS(SELECT 1 FROM companion_private.health_incremental_days d
                WHERE d.account_id=h.account_id AND d.epoch=%s AND d.day=(h.start_at AT TIME ZONE 'UTC')::date)
            LIMIT 1''',(account,page.data_epoch))).fetchone()
        if legacy:
            raise DomainError('invalid_request',409,'Legacy step totals have no UUID contributions; a reviewed baseline migration is required.')
    if any(admitted_provenance(s)=='synthetic' for s in current_samples):
        duplicate = await (await conn.execute('SELECT 1 FROM companion_private.pattern_batches WHERE account_id=%s AND dataset=%s',(account,DATASET))).fetchone()
        if duplicate:
            raise DomainError('invalid_request',409,'Remove the old operator Health dataset before importing helper records.')
    counts = dict(upserted=0,deleted=0,excluded=0,expired=len(page.samples)-len(current_samples),superseded=0,unknown_deletions=0)
    affected = set()
    # Expired additions are acknowledged by the page's durable digest/receipt only.
    # Never reconstruct their raw rows, UUID mapping, source batches or projections.
    for sample in current_samples:
        provenance = admitted_provenance(sample)
        raw = sample.model_dump(mode='json') if provenance else None
        existing = await (await conn.execute('SELECT raw,deleted,excluded FROM companion_private.health_incremental_objects WHERE account_id=%s AND epoch=%s AND hk_type=%s AND hk_uuid=%s',(account,page.data_epoch,page.hk_type,sample.hk_uuid))).fetchone()
        if existing:
            if existing[1]:
                counts['superseded'] += 1
                continue  # A known deletion always wins over an old addition.
            if existing[0] != raw or existing[2] != (provenance is None):
                raise DomainError('idempotency_conflict',409,'Health UUID contents are immutable.')
            counts['excluded' if provenance is None else 'upserted'] += 1
            continue
        batch = sample_id = None
        if provenance is None:
            counts['excluded'] += 1
        elif sample.hk_type == 'stepCount' and provenance == 'realHealthKit':
            affected.update(contribution_days(sample)); counts['upserted'] += 1
        else:
            batch,sample_id = await upsert_normalized(conn,account,normalized(sample)); counts['upserted'] += 1
        await conn.execute('''INSERT INTO companion_private.health_incremental_objects
            (account_id,epoch,hk_type,hk_uuid,raw,start_at,end_at,excluded,batch_id,sample_id,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (account,page.data_epoch,page.hk_type,sample.hk_uuid,Jsonb(raw) if raw else None,
             sample.start_at if raw else None,sample.end_at if raw else None,provenance is None,batch,sample_id,now))
    legacy_bundles = None
    for identifier in page.deleted_uuids:
        existing = await (await conn.execute('SELECT raw,deleted,batch_id,sample_id FROM companion_private.health_incremental_objects WHERE account_id=%s AND epoch=%s AND hk_type=%s AND hk_uuid=%s',(account,page.data_epoch,page.hk_type,identifier))).fetchone()
        if existing:
            raw,deleted,batch,sample_id = existing
            if not deleted:
                if raw:
                    sample = IncrementalSample.model_validate_json(json.dumps(raw))
                    if sample.hk_type=='stepCount' and classify(sample)=='realHealthKit':
                        affected.update(contribution_days(sample))
                if batch and sample_id:
                    await conn.execute('UPDATE companion_private.health_samples SET deleted=true,revision=revision+1 WHERE account_id=%s AND batch_id=%s AND sample_id=%s AND NOT deleted',(account,batch,sample_id))
                await conn.execute('UPDATE companion_private.health_incremental_objects SET deleted=true,updated_at=%s WHERE account_id=%s AND epoch=%s AND hk_type=%s AND hk_uuid=%s',(now,account,page.data_epoch,page.hk_type,identifier))
        else:
            # UUID-only tombstones are durable even when deletion precedes its addition.
            # Legacy UUID-keyed rows, including helper daily records, allow exact recovery.
            # Real daily projections use a different ID formula and are never guessed here.
            batch = sample_id = None
            if legacy_bundles is None:
                legacy_bundles = await (await conn.execute('SELECT DISTINCT h.source_bundle,h.batch_id FROM companion_private.health_samples h JOIN companion_private.pattern_batches b USING(account_id,batch_id) WHERE h.account_id=%s AND h.hk_type=%s AND b.origin=\'healthkit\' LIMIT 1001',(account,page.hk_type))).fetchall()
                if len(legacy_bundles) > 1000:
                    raise DomainError('invalid_request',422,'Legacy source mapping exceeds the bounded lookup.')
            if legacy_bundles:
                matches = await (await conn.execute('''SELECT batch_id,sample_id FROM companion_private.health_samples
                    WHERE account_id=%s AND (batch_id,sample_id) IN
                    (SELECT * FROM unnest(%s::uuid[],%s::text[]))''',
                    (account,[b for _,b in legacy_bundles],[stable(bundle+':'+identifier) for bundle,_ in legacy_bundles]))).fetchall()
                if len(matches)>1:
                    raise DomainError('idempotency_conflict',409,'Ambiguous legacy UUID identity.')
                if matches:
                    batch,sample_id = matches[0]
                    await conn.execute('UPDATE companion_private.health_samples SET deleted=true,revision=revision+1 WHERE account_id=%s AND batch_id=%s AND sample_id=%s AND NOT deleted',(account,batch,sample_id))
            await conn.execute('INSERT INTO companion_private.health_incremental_objects(account_id,epoch,hk_type,hk_uuid,deleted,batch_id,sample_id,updated_at) VALUES(%s,%s,%s,%s,true,%s,%s,%s)',(account,page.data_epoch,page.hk_type,identifier,batch,sample_id,now))
            if sample_id is None: counts['unknown_deletions'] += 1
        counts['deleted'] += 1
    count = await (await conn.execute('SELECT count(*) FROM companion_private.health_incremental_objects WHERE account_id=%s AND epoch=%s',(account,page.data_epoch))).fetchone()
    if count[0] > MAX_OBJECTS:
        raise DomainError('invalid_request',422,'Health UUID retention bound reached; reconcile retention before retrying.')
    finished = ready or not page.has_more
    if page.hk_type=='stepCount' and finished:
        if not ready:
            rows = await (await conn.execute('SELECT raw FROM companion_private.health_incremental_objects WHERE account_id=%s AND epoch=%s AND hk_type=\'stepCount\' AND raw IS NOT NULL AND NOT excluded',(account,page.data_epoch))).fetchall()
            for (raw,) in rows:
                sample = IncrementalSample.model_validate_json(json.dumps(raw))
                if classify(sample)=='realHealthKit': affected.update(contribution_days(sample))
        for day in sorted(affected):
            await recompute_day(conn,account,page.data_epoch,day,now)
    await conn.execute('''INSERT INTO companion_private.health_incremental_streams(account_id,epoch,hk_type,stream_id,next_sequence,bootstrapped)
        VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(account_id,epoch,hk_type)
        DO UPDATE SET next_sequence=excluded.next_sequence,bootstrapped=excluded.bootstrapped''',
        (account,page.data_epoch,page.hk_type,page.stream_id,page.sequence+1,finished))
    response = HealthIncrementalReceipt(schema_version='1',request_id=page.request_id,data_epoch=page.data_epoch,
        stream_id=page.stream_id,sequence=page.sequence,hk_type=page.hk_type,bootstrapped=finished,**counts).model_dump(mode='json')
    await conn.execute('INSERT INTO companion_private.health_incremental_acknowledgments(account_id,epoch,request_id,digest,response) VALUES(%s,%s,%s,%s,%s)',
        (account,page.data_epoch,page.request_id,digest,Jsonb(response)))
    await conn.execute('INSERT INTO companion_private.receipts(account_id,epoch,route,request_id,digest,response,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)',
        (account,page.data_epoch,ROUTE,page.request_id,digest,Jsonb(response),now))
    return response


async def ingest_health_incremental(service, body):
    """Ready-to-wire authenticated route handler; returns a typed receipt, NOT State.

    Uses the same service mutex and account persistence transaction as pattern reads.
    No state replacement, provider call, independent pool, or implicit schema migration.
    The shared outbox must decode HealthIncrementalReceipt for this route.
    """
    if isinstance(body, HealthIncrementalRequest):
        body = body.model_dump(mode='json')
    async with service.lock:
        coordinator = getattr(service,'pattern_coordinator',None)
        if coordinator is None:
            raise DomainError('not_configured',409)
        async with coordinator.transaction() as conn:
            return await stage_health_incremental(conn,coordinator.store.account_id,body,now=service.clock())
