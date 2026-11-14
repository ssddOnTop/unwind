"""Non-executable, server-owned workflow capabilities and presentation."""
from datetime import datetime,timezone,timedelta
from dataclasses import dataclass
import os
import hashlib,json
from uuid import UUID
from typing import Literal
from pydantic import Field,model_validator
from .patterns import Strict,PatternOverview

@dataclass(frozen=True)
class WorkflowConfig:
    enabled: bool=False
    manual_accounts: frozenset[str]=frozenset()

    @classmethod
    def from_env(cls):
        mode=os.environ.get('COMPANION_PATTERN_WORKFLOWS','disabled')
        if mode not in {'enabled','disabled'}:raise ValueError('Invalid workflow mode')
        if mode=='enabled' and os.environ.get('COMPANION_DAILY_SYNTHESIS_MODE','disabled')!='disabled':raise ValueError('Legacy daily synthesis must remain disabled')
        raw=os.environ.get('COMPANION_PATTERN_MANUAL_ACCOUNTS','')
        return cls(mode=='enabled',frozenset(str(UUID(v.strip())) for v in raw.split(',') if v.strip()))


def eligible(doc):
    state=doc.get('state',{})
    return bool(state.get('preferences',{}).get('cloud_consent') and any(s.get('id') in {'healthkit','plaid'} and s.get('enabled') and s.get('consent')=='granted' for s in state.get('sources',[])))

def consent_key(doc):
    state=doc.get('state',{})
    value={'cloud':state.get('preferences',{}).get('cloud_consent'),
           'sources':[(s['id'],s.get('enabled'),s.get('consent'),s.get('read_access')) for s in state.get('sources',[])],
           'banks':doc.get('meta',{}).get('banking_item_contexts',[])}
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

class EnqueueRequest(Strict):
    schema_version: Literal['1']
    request_id: str
    data_epoch: str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$')
    @model_validator(mode='after')
    def valid(self):
        UUID(self.request_id)
        return self

class WorkflowAction(Strict):
    id: Literal['enqueue']
    label: str

class WorkflowPage(Strict):
    id: Literal['home','ledger','brief','settings']
    title: str
    paragraphs: list[str]

class WorkflowChoice(Strict):
    id: Literal['for_me','not_for_me','unsure']
    label: str

class WorkflowQuestion(Strict):
    id: Literal['attribution']
    label: str
    choices: list[WorkflowChoice]

class WorkflowJob(Strict):
    id: str
    utc_date: str
    status: Literal['queued','running','complete','unavailable','cancelled','failed']
    message: str
    bank_status: Literal['not_checked','saved','refreshed','partial','unavailable']
    updated_at: str
    bank_message: str

class WorkflowOverview(Strict):
    schema_version: Literal['1']='1'
    enabled: bool
    eligible: bool
    next_scheduled_at: str
    consent_label: str
    consent_notice: str
    action: WorkflowAction | None
    pages: list[WorkflowPage]
    questions: list[WorkflowQuestion]
    job: WorkflowJob | None
    report: PatternOverview


def overview(config,account,doc,now,report,job=None):
    consent=eligible(doc)
    tomorrow=(now.astimezone(timezone.utc)+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
    schedule='Patterns are queued daily at 00:00 UTC. Only a pattern summary is sent for AI review.'
    readiness='Nightly review is enabled for your permitted sources.' if consent else 'Enable cloud review and at least one permitted Health or bank source to allow nightly review.'
    if not config.enabled:readiness='Nightly workflows are not enabled on this server.'
    return WorkflowOverview(enabled=config.enabled,eligible=consent,next_scheduled_at=tomorrow.isoformat(),
        consent_label='Allow nightly cloud review',consent_notice='Off by default. Allow coarse pattern summaries from permitted Health and bank sources to be reviewed at 00:00 UTC. Raw records, identity and private notes are not sent. Summaries remain sensitive; you may turn this off anytime.',
        action=WorkflowAction(id='enqueue',label='Queue my pattern review') if config.enabled and consent and account in config.manual_accounts else None,
        pages=[WorkflowPage(id='home',title='Current assessment',paragraphs=[schedule,readiness]),
               WorkflowPage(id='ledger',title='Current assessment',paragraphs=['One latest result per area. Earlier assessments remain in private history, not as current warnings. Missing coverage is unknown, not good health.']),
               WorkflowPage(id='brief',title='Prepare an optional doctor question',paragraphs=['Your saved notes remain private and are never sent for AI review.']),
               WorkflowPage(id='settings',title='Nightly pattern review',paragraphs=[schedule,readiness,'Bank data follows its rolling 24-hour refresh limit. Missing or older data is not proof of no activity.','Your private backend owns imported records and reviews. This device retains a protected cache and unsent Health uploads.'])],
        questions=[WorkflowQuestion(id='attribution',label='Were these records mostly about you?',choices=[WorkflowChoice(id='for_me',label='Mostly about me'),WorkflowChoice(id='not_for_me',label='Not mostly about me'),WorkflowChoice(id='unsure',label='Not sure')])],job=job,report=report)
