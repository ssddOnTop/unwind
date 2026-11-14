"""Manual, bounded pattern review. Only Aggregate may cross the model boundary."""
from __future__ import annotations
import asyncio
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = 'patterns-1:aggregate-1:prompt-1'
NOTICE = ('Only a coarse pattern summary is sent for AI review, not identities, merchants or raw records. '
          'Aggregates can still be sensitive. Missing data is unknown, not zero. These are observations, not diagnoses or causes.')
COPY = {
 'activity': ('Lower recorded activity', 'Recent recorded daily steps are lower than the baseline. Consider whether your routine or recording coverage changed.'),
 'sleep': ('Shorter recorded sleep', 'Recent recorded asleep intervals are shorter than the baseline. This is not a diagnosis and may reflect missing recordings.'),
 'food_service': ('More food-service purchases', 'Food-service purchase frequency increased. Purchases do not establish who ate the food, nutrition or diet quality.'),
}

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class AnalyzeRequest(Strict):
    schema_version: Literal['1']
    request_id: str
    @model_validator(mode='after')
    def valid(self):
        UUID(self.request_id)
        return self

class HealthSample(Strict):
    sample_id: str = Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9_.:-]+$')
    hk_type: Literal['stepCount','sleepAnalysis','heartRate','restingHeartRate','workout']
    kind: Literal['dailyCumulative','category','quantity','workout']
    start_at: datetime
    end_at: datetime
    timezone: str
    value: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unit: Literal['count','bpm'] | None = None
    category: Literal['inBed','asleepCore','asleepDeep','asleepREM','awake'] | None = None
    workout_activity: Literal['walking','running','cycling'] | None = None
    duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    energy_kcal: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    distance_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    source_name: str = Field(min_length=1, max_length=80)
    revision: int = Field(default=1, ge=1)
    @model_validator(mode='after')
    def valid(self):
        ZoneInfo(self.timezone)
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None or self.end_at < self.start_at:
            raise ValueError('Timezone-aware ordered intervals required')
        if self.hk_type == 'stepCount':
            ok = self.kind == 'dailyCumulative' and self.unit == 'count' and self.value is not None and self.end_at > self.start_at
        elif self.hk_type in {'heartRate','restingHeartRate'}:
            ok = self.kind == 'quantity' and self.unit == 'bpm' and self.value is not None
        elif self.hk_type == 'sleepAnalysis':
            ok = self.kind == 'category' and self.category is not None and self.end_at > self.start_at
        else:
            ok = self.kind == 'workout' and self.workout_activity is not None and self.duration_seconds is not None and self.end_at > self.start_at
            if ok and self.duration_seconds > (self.end_at-self.start_at).total_seconds():
                ok = False
        if not ok: raise ValueError('Invalid normalized HealthKit semantics')
        return self

class Transaction(Strict):
    transaction_id: str = Field(min_length=1, max_length=160, pattern=r'^[A-Za-z0-9_.:-]+$')
    event_at: datetime
    timezone: str
    amount: float = Field(allow_inf_nan=False)
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    category: Literal['food_service','groceries','other']
    @model_validator(mode='after')
    def valid(self):
        ZoneInfo(self.timezone)
        if self.event_at.tzinfo is None: raise ValueError('Timezone required')
        return self

class Period(Strict):
    label: Literal['recent4weeks','baseline']
    steps_average_bucket: int | None = Field(ge=0, le=50000, multiple_of=1000)
    sleep_hours_bucket: float | None = Field(ge=0, le=24, multiple_of=0.5)
    food_service_per_week_bucket: int = Field(ge=0, le=14)
    workouts_per_week_bucket: int = Field(ge=0, le=14)
    missing_steps: bool
    missing_sleep: bool
    missing_transactions: bool

class Aggregate(Strict):
    sample_count_bucket: Literal['none','under100','100to999','1000plus']
    recent: Period
    baseline: Period

class ClarifyRequest(Strict):
    schema_version: Literal['1']
    run_id: str
    concern_id: Literal['activity','sleep','food_service']
    attribution: Literal['for_me','not_for_me','unsure']
    @model_validator(mode='after')
    def valid(self):
        UUID(self.run_id)
        return self

class Finding(Strict):
    concern_id: Literal['activity','sleep','food_service']
    title: str
    explanation: str
    evidence_codes: list[Literal['steps_comparison','sleep_comparison','purchase_frequency']]
    confidence: Literal['limited','moderate']
    attribution: Literal['unconfirmed','for_me','not_for_me','unsure'] = 'unconfirmed'
    evidence_summary: str

class PatternOverview(Strict):
    schema_version: Literal['1'] = '1'
    status: Literal['not_analyzed','running','complete','unavailable']
    analyzed_at: str | None
    run_id: str | None
    engine: Literal['deterministic','LLMassisted']
    is_demo: bool
    can_analyze: bool
    source_label: str
    doctor_brief: str
    privacy_notice: str = NOTICE
    evidence: Aggregate | None
    findings: list[Finding]


def aggregate(samples, transactions, now):
    """Local exact interval arithmetic; the outbound result is coarse and fixed-shape."""
    now = now.astimezone(timezone.utc)
    boundary = now - timedelta(days=28)
    beginning = now - timedelta(days=168)
    periods = []
    for label, start, end, days in [('recent4weeks',boundary,now,28), ('baseline',beginning,boundary,140)]:
        steps = defaultdict(float)
        sleep = []
        workouts = 0
        for s in samples:
            a,b = s['start_at'],s['end_at']
            # Complete daily aggregates only; point samples are never misrepresented as daily totals.
            if s['hk_type']=='stepCount' and start <= a and b <= end:
                steps[a.astimezone(ZoneInfo(s['timezone'])).date()] += s['value']
            if s['hk_type']=='sleepAnalysis' and s['category'] in {'asleepCore','asleepDeep','asleepREM'}:
                a,b = max(a,start),min(b,end)
                if b > a: sleep.append((a,b))
            if s['hk_type']=='workout' and start <= a < end: workouts += 1
        # Union globally before grouping, including overlapping stages across midnight.
        merged=[]
        for a,b in sorted(sleep):
            if merged and a <= merged[-1][1]: merged[-1]=(merged[-1][0],max(merged[-1][1],b))
            else: merged.append((a,b))
        nightly=defaultdict(float)
        for a,b in merged:
            nightly[b.astimezone(timezone.utc).date()]+=(b-a).total_seconds()/3600
        hours=list(nightly.values())
        tx=[t for t in transactions if start <= t['event_at'] < end]
        periods.append(Period(label=label,
            steps_average_bucket=min(50000,int(sum(steps.values())/len(steps)//1000)*1000) if steps else None,
            sleep_hours_bucket=min(24.0,math.floor(sum(hours)/len(hours)*2)/2) if hours else None,
            food_service_per_week_bucket=min(14,round(sum(t['category']=='food_service' and t['amount']>0 for t in tx)*7/days)),
            workouts_per_week_bucket=min(14,round(workouts*7/days)),
            missing_steps=len(steps)<days*.8,missing_sleep=len(hours)<days*.8,missing_transactions=not bool(tx)))
    n=len(samples)+len(transactions)
    return Aggregate(sample_count_bucket='none' if not n else 'under100' if n<100 else '100to999' if n<1000 else '1000plus',recent=periods[0],baseline=periods[1])


def concerns(evidence):
    r,b = evidence.recent,evidence.baseline
    ids=[]
    if r.steps_average_bucket is not None and b.steps_average_bucket and r.steps_average_bucket <= b.steps_average_bucket*.7: ids.append('activity')
    if r.sleep_hours_bucket is not None and b.sleep_hours_bucket is not None and r.sleep_hours_bucket <= b.sleep_hours_bucket-1: ids.append('sleep')
    if not r.missing_transactions and not b.missing_transactions and r.food_service_per_week_bucket >= b.food_service_per_week_bucket+2: ids.append('food_service')
    return ids


def findings(ids, evidence):
    codes={'activity':'steps_comparison','sleep':'sleep_comparison','food_service':'purchase_frequency'}
    r,b=evidence.recent,evidence.baseline
    summaries={
        'activity': f'Recent 4 weeks: about {r.steps_average_bucket} steps per recorded day; baseline: about {b.steps_average_bucket}. Workout frequency: {r.workouts_per_week_bucket} versus {b.workouts_per_week_bucket} per week.',
        'sleep': f'Recent 4 weeks: about {r.sleep_hours_bucket} recorded asleep hours per recorded night; baseline: about {b.sleep_hours_bucket}. Overlapping sleep stages are counted once; in-bed time is excluded.',
        'food_service': f'Recent 4 weeks: about {r.food_service_per_week_bucket} food-service purchases per week; baseline: about {b.food_service_per_week_bucket}. No merchant names or nutrition assumptions are used.'}
    return [Finding(concern_id=i,title=COPY[i][0],explanation=COPY[i][1],evidence_codes=[codes[i]],evidence_summary=summaries[i],
        confidence='limited' if i=='food_service' or ((r.missing_steps or b.missing_steps) if i=='activity' else (r.missing_sleep or b.missing_sleep)) else 'moderate') for i in ids]


async def model_review(evidence: Aggregate, allowed, model_factory):
    """One invocation, no tool binding, no retries, no dynamic prose accepted from the model."""
    # Round-trip through the allowlist, rather than serializing any persistence row.
    dto=Aggregate.model_validate(evidence.model_dump()).model_dump()
    model=await asyncio.wait_for(asyncio.to_thread(model_factory),timeout=6)
    if model is None: raise ValueError('Unavailable')
    from langchain_core.messages import SystemMessage, HumanMessage
    result=await asyncio.wait_for(model.ainvoke([
        SystemMessage(content='Rank the supported pattern IDs for a non-diagnostic review. Do not infer disease, causality, consumption or identity. Output ONLY JSON {"concern_ids":[...]} selecting each supported ID once. Supported IDs are activity (lower steps), sleep (shorter recorded sleep), food_service (more purchases). Never add a new ID. No tools.'),
        HumanMessage(content=json.dumps(dto,sort_keys=True,separators=(',',':')))],
        max_tokens=300, config={'callbacks': []}),timeout=12)
    content=result.content
    if not isinstance(content,str) or len(content)>2000: raise ValueError('Invalid review')
    value=json.loads(content)
    if set(value)!={'concern_ids'} or not isinstance(value['concern_ids'],list): raise ValueError('Invalid review')
    ids=value['concern_ids']
    if any(not isinstance(i,str) for i in ids) or len(ids)!=len(set(ids)) or set(ids)!=set(allowed): raise ValueError('Unsupported evidence')
    return ids


def empty_overview():
    return PatternOverview(status='not_analyzed',analyzed_at=None,run_id=None,engine='deterministic',is_demo=False,can_analyze=False,
        source_label='No normalized dataset loaded',doctor_brief='',evidence=None,findings=[]).model_dump()
