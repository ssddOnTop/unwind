"""Multi-Item application transaction adapter; core remains registry-only."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from .contracts import DomainError
from .domain import defaults,iso,instant,preference_update
from .banking_contracts import validate_banking


def result(status,message,**fields):
    return dict(status=status,message=message,link_session_id=None,link_token=None,expiration=None,added=None,modified=None,removed=None)|fields


class BankingCoordinator:
    route='/banking/v1/actions'

    def __init__(self,service):
        self.service=service;self.tasks={};self.generation=0;self.deleting=False

    @property
    def busy(self):return bool(self.tasks)

    def source(self,doc):return next(s for s in doc['state']['sources'] if s['id']=='plaid')

    def payload(self,doc):return {'vault':doc['meta'].get('banking_vault'),'state':deepcopy(doc['state']),'now':iso(self.service.clock())}

    async def call(self,operation,payload):return await self.service.registry.dispatch('banking',operation,payload)

    def initialize(self,doc):
        source=self.source(doc)
        source['capability']=next(m['status'] for m in self.service.registry.metadata() if m['id']=='banking')
        if source['consent']=='not_requested':
            source.update(status='disabled' if source['capability']=='available' else 'not_configured',reason='Plaid Sandbox bank management; no bank connected.')

    async def recover(self,doc):
        self.initialize(doc)
        if not doc['meta'].get('banking_vault'):
            upgraded=await self.call('upgrade',self.payload(doc)|{'legacy_vault':doc['meta'].get('plaid_vault'),'legacy_connected':doc['meta'].get('plaid_connected',False)})
            doc['meta']['banking_vault']=upgraded['vault']
            doc['meta'].pop('plaid_vault',None)
            doc['meta'].pop('plaid_item_id',None)
        doc['meta'].pop('banking_inflight',None)

    async def prepare(self,doc):
        projected=await self.call('project',self.payload(doc));ctx=projected['context'];meta=doc['meta']
        meta.update(pattern_bank_accounts=ctx.get('pattern_accounts',[]),banking_excluded_ids=ctx['excluded_ids'],banking_item_contexts=ctx['items'],banking_selected_count=ctx['selected_count'],plaid_connected=ctx['connected'])
        source=self.source(doc)
        if ctx['connected']:
            source.update(enabled=True,consent='granted',authorization_requested=True,read_access='not_applicable',
                status='ready' if ctx['ready'] else 'syncing' if ctx['selected_count'] else 'empty',coverage='partial' if ctx['ready'] else 'unknown',
                coverage_window=None,last_success_at=ctx['last_success_at'],reason='Synthetic Sandbox records from Plaid-shared accounts only; no-longer-shared account history is excluded from new insights.')
            enabled=doc['state']['preferences']['enabled_source_ids']
            if 'plaid' not in enabled:enabled.append('plaid')
        else:
            source.update(enabled=False,status='disabled' if source['capability']=='available' else 'not_configured',coverage='unknown',coverage_window=None)
            doc['state']['preferences']['enabled_source_ids']=[s for s in doc['state']['preferences']['enabled_source_ids'] if s!='plaid']

    async def get(self):
        await self.service.housekeep()
        async with self.service.lock:
            value=(await self.call('project',self.payload(self.service.document)))['view']
            validate_banking('banking',value);return value

    async def prune_private(self,doc):
        if doc['meta'].get('banking_vault'):
            outcome=await self.call('prune',self.payload(doc)|{'observation_ids':[o['id'] for o in doc['state']['observations']]})
            doc['meta']['banking_vault']=outcome['vault']

    async def invalidate(self):self.generation+=1

    async def request(self,body,**kwargs):
        # Replay already committed v1 receipts after upgrade, but never let the
        # single-Item writer overwrite a multi-Item map.
        async with self.service.lock:
            if body['data_epoch']!=self.service.document['state']['data_epoch']:raise DomainError('epoch_mismatch',409)
            receipt=self.service.receipt('/connections/plaid',body)
            receipt['digest']=(await self.call('digest',body))['digest']
            cached=await self.service.registry.dispatch('persistence','receipt',receipt)
            if cached:
                if cached['digest']!=receipt['digest']:raise DomainError('idempotency_conflict',409)
                if cached['response'] is None:raise DomainError('busy',409)
                return await self.call('receipt_open',{'ciphertext':cached['response']['plaid_receipt']})
        raise DomainError('busy',409,'Use versioned bank management for this profile.')

    async def before_preferences(self,body):
        service=self.service
        async with service.lock:
            receipt=service.receipt('/preferences',body)
            if await service.replay(receipt) is not None:return
            trial=deepcopy(service.document['state'])
            preference_update(trial,body['preferences'],allow_plaid=bool(service.document['meta'].get('plaid_connected')))
            if 'plaid' in body['preferences']['enabled_source_ids']:return
            doc=deepcopy(service.document)
            outcome=await self.call('revoke_all',self.payload(doc))
            if not outcome['items'] and not doc['meta'].get('banking_inflight'):return
            self.generation+=1;epoch=doc['state']['data_epoch']
            doc['meta']['banking_vault']=outcome['vault'];doc['state']['preferences']=trial['preferences']
            service.invalidate(doc['state']);await service.cancel_synthesis();await service.clear_checkpoints()
            await service.evaluate(doc);await service.commit(doc)
        # Authority for every Item is already fenced in one transaction.
        for item in outcome['items']:
            confirmed=(await self.call('remove',{'item':item}))['confirmed']
            async with service.lock:
                if service.document['state']['data_epoch']!=epoch:raise DomainError('epoch_mismatch',409)
                doc=deepcopy(service.document)
                final=await self.call('disconnect_done',self.payload(doc)|{'connection_id':item['id'],'confirmed':confirmed})
                doc['meta']['banking_vault']=final['vault'];await service.commit(doc)

    async def publish(self,doc,receipt,reply,pattern_changes=None):
        doc['state']['revision']+=1;doc['state']['generated_at']=iso(self.service.clock())
        await self.service.evaluate(doc)
        value=(await self.call('project',self.payload(doc)))['view'];value['result']=reply
        validate_banking('banking',value)
        sealed=await self.call('receipt_seal',value)
        receipt.update(response={'banking_receipt':sealed['ciphertext']},preserve_response=True)
        await self.service.commit(doc,receipt,bump=False,pattern_bank_changes=pattern_changes)
        return value

    async def action(self,body,*,admission_guard=None):
        validate_banking('request',body);service=self.service;action=body['action'];run_id=str(uuid4())
        local=action=='cancel_link'
        async with service.lock:
            if self.deleting:raise DomainError('busy',409)
            generation=self.generation
            receipt=service.receipt(self.route,body)
            if body['data_epoch']!=service.document['state']['data_epoch']:raise DomainError('epoch_mismatch',409)
            receipt['digest']=(await self.call('digest',body))['digest']
            cached=await service.registry.dispatch('persistence','receipt',receipt)
            if cached:
                if cached['digest']!=receipt['digest']:raise DomainError('idempotency_conflict',409)
                if cached['response'] is None:raise DomainError('busy',409)
                return await self.call('receipt_open',{'ciphertext':cached['response']['banking_receipt']})
            if body['expected_revision']!=service.document['state']['revision']:raise DomainError('stale_banking',409)
            if admission_guard is not None and not await admission_guard():raise DomainError('consent_required',409)
            if self.busy and not local and action!='disconnect':raise DomainError('busy',409)
            doc=deepcopy(service.document)
            if local:
                outcome=await self.call('local',self.payload(doc)|{'request':body})
                doc['meta']['banking_vault']=outcome['vault']
                service.invalidate(doc['state']);await service.cancel_synthesis();await service.clear_checkpoints()
                response=await self.publish(doc,receipt,result('cancelled','Link cancelled; existing sharing is unchanged. Any completed exchange is locally disabled; remote removal may remain unconfirmed.'))
                if action=='cancel_link':await self.call('finish_recovery',self.payload(service.document))
                return response
            captured=(await self.call('describe',self.payload(doc)))['items']
            if action=='disconnect':
                outcome=await self.call('disconnect_local',self.payload(doc)|{'connection_id':body['connection_id']})
                doc['meta']['banking_vault']=outcome['vault']
                service.invalidate(doc['state']);await service.cancel_synthesis();await service.clear_checkpoints()
                await service.evaluate(doc);await service.commit(doc)
                task=asyncio.create_task(self.call('remove',{'item':outcome['item']}))
            else:
                if self.source(doc)['capability']!='available':raise DomainError('not_configured',409)
                # Persist admission without holding any DB transaction over provider awaits.
                doc['meta']['fixture_session']=False
                doc['meta']['banking_inflight']={'action':action,'run_id':run_id}
                if action=='sync':self.source(doc)['last_attempt_at']=iso(service.clock())
                await service.evaluate(doc);await service.commit(doc)
                task=asyncio.create_task(self.call('network',self.payload(doc)|{'request':body}))
            self.tasks[run_id]=task;epoch=doc['state']['data_epoch']
            inputs=[(o['id'],o['revision']) for o in doc['state']['observations']]
        try:
            async with asyncio.timeout(90):outcome=await task
        except BaseException as exc:
            async with service.lock:
                self.tasks.pop(run_id,None)
                if service.document['state']['data_epoch']==epoch:
                    failed=deepcopy(service.document);failed['meta'].pop('banking_inflight',None)
                    await service.commit(failed)
            if isinstance(exc,asyncio.CancelledError):raise
            if isinstance(exc,DomainError):raise
            raise DomainError('internal_error',500,'Banking operation was not completed; no partial account cursor was published.') from None
        async with service.lock:
            self.tasks.pop(run_id,None)
            if service.document['state']['data_epoch']!=epoch:raise DomainError('epoch_mismatch',409)
            if generation!=self.generation:raise DomainError('obsolete_selection',409)
            doc=deepcopy(service.document);doc['meta'].pop('banking_inflight',None)
            if action=='disconnect':
                final=await self.call('disconnect_done',self.payload(doc)|{'connection_id':body['connection_id'],'confirmed':outcome['confirmed']})
                doc['meta']['banking_vault']=final['vault']
                reply=result('disconnected' if outcome['confirmed'] else 'removal_pending','Bank disconnected here. Saved history is kept; no bank account was closed.' if outcome['confirmed'] else 'Local imports stopped. Remote Item removal is unconfirmed; no bank account was closed.')
            else:
                merged=await self.call('merge',self.payload(doc)|{'request':body,'outcome':outcome,'captured':{k:v['generation'] for k,v in captured.items()},'input_keys':inputs})
                doc['meta']['banking_vault']=merged['vault'];doc['state']['observations']=merged['observations']
                if action in {'add_link','update_link'}:reply=result('link_ready','Open native Sandbox Link.',link_session_id=outcome['session_id'],link_token=outcome['link_token'],expiration=outcome['expiration'])
                elif merged.get('cancelled'):reply=result('cancelled','Link was cancelled. Local imports are stopped; remote Item removal is unconfirmed. Use Disconnect bank to retry removal.')
                else:reply=result('partial' if merged['partial'] else 'complete','Some shared account imports or inventory need attention.' if merged['partial'] else 'Bank information updated. All accounts returned by Plaid shared inventory are enabled.',**merged['counts'])
            service.invalidate(doc['state']);await service.clear_checkpoints()
            response=await self.publish(doc,receipt,reply,merged.get('pattern_changes',[]) if action!='disconnect' else [])
            if action=='exchange':await self.call('committed',{})
            return response

    async def sync_item(self,item_id,*,admission_guard=None):
        async with self.service.lock:
            doc=self.service.document;described=await self.call('describe',self.payload(doc))
            match=next((cid for cid,i in described['items'].items() if i['item_id']==item_id and i['connected']),None)
            if not match:return None
            body={'schema_version':'1','request_id':'item-sync:'+str(uuid4()),'data_epoch':doc['state']['data_epoch'],
                'expected_revision':doc['state']['revision'],'action':'sync','connection_id':match}
        return await self.action(body,admission_guard=admission_guard)

    async def daily_sync(self):
        async with self.service.lock:
            doc=self.service.document
            if self.busy or not doc['meta'].get('plaid_connected') or not doc['meta'].get('banking_selected_count'):return
            source=self.source(doc)
            if source['last_attempt_at'] and self.service.clock()-instant(source['last_attempt_at'])<timedelta(hours=24):return
            body={'schema_version':'1','request_id':'banks-daily:'+str(uuid4()),'data_epoch':doc['state']['data_epoch'],
                'expected_revision':doc['state']['revision'],'action':'sync','connection_id':None}
        try:return await self.action(body)
        except DomainError:return None

    async def delete(self,body):
        async with self.service.lock:
            if self.deleting:raise DomainError('busy',409)
            self.deleting=True
        cleanup=asyncio.create_task(self._delete(body))
        try:return await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
        finally:self.deleting=False

    async def _delete(self,body):
        service=self.service;receipt=service.receipt('/data',body)
        async with service.lock:
            replay=await service.replay(receipt)
            if replay is not None:return replay
            captured=(await self.call('list_private',self.payload(service.document)))['items']
            ambiguous=service.document['meta'].get('banking_inflight',{}).get('action')=='exchange'
            self.generation+=1
            for task in tuple(self.tasks.values()):task.cancel()
            doc={'state':defaults(service.clock()),'meta':{'fixture_session':False,'issued':[]}}
            self.initialize(doc)
            doc['state']['limitations'].append('Remote Sandbox Item removal unconfirmed.')
            await service.cancel_synthesis();await service.clear_checkpoints();await service.evaluate(doc)
            await service.commit(doc,erase=True);epoch=doc['state']['data_epoch']
        confirmed=not ambiguous
        for item in captured:
            if item.get('access_token'):
                try:confirmed &= (await self.call('remove',{'item':item}))['confirmed']
                except Exception:confirmed=False
        await self.call('clear',{})
        async with service.lock:
            if service.document['state']['data_epoch']!=epoch:return deepcopy(service.document['state'])
            doc=deepcopy(service.document)
            if confirmed:doc['state']['limitations']=[v for v in doc['state']['limitations'] if v!='Remote Sandbox Item removal unconfirmed.']
            receipt['response']=doc['state'];await service.commit(doc,receipt,erase=True)
            return deepcopy(receipt['response'])
