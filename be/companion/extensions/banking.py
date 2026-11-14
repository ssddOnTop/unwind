"""Replaceable multi-Item Sandbox capability. Private inventory never enters State/model input."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import hashlib
from uuid import uuid4

from ..contracts import DomainError
from ..connections.plaid import PlaidFailure
from ..domain import instant
from ..plaid_projection import project
from ..vault import PrivateVault

NOTICE='Removing an account here stops future imports and excludes its saved records from new insights. Saved history stays in your ledger. This is a local setting, not bank-side access revocation.'


def text(value,limit,default=None):
    if not isinstance(value,str):return default
    value=''.join(c for c in value if ord(c)>=32 and ord(c)!=127).strip()
    return value[:limit] or default


def empty():return {'version':2,'items':{},'sessions':{}}


def group_id(kind,subtype):
    return 'group:'+hashlib.sha256((kind+'\0'+str(subtype)).encode()).hexdigest()[:32]


class BankingExtension:
    id,version='banking','1'

    def __init__(self,data_dir,client,*,webhook_url=None,client_user_id=None,ownership_check=None,claim_item=None):
        self.client,self.vault=client,PrivateVault(data_dir)
        self.webhook_url,self.client_user_id,self.ownership_check=webhook_url,client_user_id,ownership_check
        self.claim_item=claim_item
        self.live_versions={};self.live_epoch=None
        if webhook_url:
            from urllib.parse import urlsplit
            url=urlsplit(webhook_url)
            if url.scheme!='https' or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError('Webhook URL must be configured HTTPS.')

    @property
    def status(self):return 'available' if self.client.configured else 'not_configured'

    def open(self,ciphertext):
        value=self.vault.open(ciphertext)
        if not value:return empty()
        if value.get('version')!=2:raise RuntimeError('Unsupported banking private format')
        return value

    async def inventory(self,item,now):
        data=await self.client.request('/accounts/get',{'access_token':item['access_token']})
        rows,provider_item=data.get('accounts'),data.get('item')
        if not isinstance(rows,list) or len(rows)>1000 or not isinstance(provider_item,dict) or provider_item.get('item_id')!=item['item_id']:
            raise PlaidFailure()
        result=deepcopy(item)
        old=result['accounts'];seen=set()
        for row in rows:
            raw=row.get('account_id') if isinstance(row,dict) else None
            if not isinstance(raw,str) or not 1<=len(raw)<=256 or raw in seen:raise PlaidFailure()
            seen.add(raw)
            prior=old.get(raw)
            account=deepcopy(prior) if prior else {'id':str(uuid4()),'enabled':False,'stream':{'cursor':None,'records':{},'status':'not_synced','last_sync_at':None}}
            account.update(name=text(row.get('name'),160,'Unnamed account'),official_name=text(row.get('official_name'),240),
                mask=row.get('mask') if isinstance(row.get('mask'),str) and len(row['mask'])<=16 else None,
                type=text(row.get('type'),80,'unknown'),subtype=text(row.get('subtype'),100),availability='available')
            old[raw]=account
        for raw,account in old.items():
            if raw not in seen:
                account.update(availability='missing',enabled=False)
                account['stream']['status']='disabled'
        result['institution_name']=text(provider_item.get('institution_name'),180)
        result['last_inventory_at']=now
        result['status']='connected'
        return result

    async def remove(self,item):
        token=item.get('access_token')
        if not token:return True
        try:
            if self.ownership_check is not None and not await self.ownership_check(item.get('item_id')):return False
            await asyncio.wait_for(self.client.request('/item/remove',{'access_token':token}),5)
            return True
        except Exception:return False

    def view(self,private,state,result=None):
        connections=[]
        for item in private['items'].values():
            accounts=[];groups={}
            for value in item['accounts'].values():
                available=value['availability'] if item['connected'] else 'disconnected'
                accounts.append({k:deepcopy(value[k]) for k in ('id','name','official_name','mask','type','subtype')}|{
                    'availability':available,'enabled':value['enabled'] and item['connected'] and available=='available',
                    'can_enable':item['connected'] and available=='available','import_status':value['stream']['status'] if value['enabled'] and item['connected'] else 'disabled',
                    'last_sync_at':value['stream']['last_sync_at']})
                if available=='available':
                    gid=group_id(value['type'],value['subtype'])
                    groups.setdefault(gid,{'id':gid,'label':value['subtype'] or value['type'],'account_ids':[]})['account_ids'].append(value['id'])
            for group in groups.values():group['account_ids'].sort()
            connections.append({'id':item['id'],'label':item.get('institution_name') or 'Connected bank','institution_name':item.get('institution_name'),
                'status':item['status'] if item['connected'] else 'disconnected','can_manage':item['connected'],
                'can_disconnect':item['connected'] or item.get('removal_pending',False),'last_inventory_at':item.get('last_inventory_at'),
                'last_sync_at':item.get('last_sync_at'),'accounts':sorted(accounts,key=lambda a:a['id']),'groups':sorted(groups.values(),key=lambda g:g['id'])})
        status='not_configured' if self.status!='available' else 'attention' if any(i['status']=='attention' or i.get('removal_pending') for i in private['items'].values()) else 'ready' if any(i['connected'] for i in private['items'].values()) else 'not_connected'
        return {'schema_version':'1','data_epoch':state['data_epoch'],'revision':state['revision'],'generated_at':state['generated_at'],
            'environment':'sandbox','status':status,'selection_notice':NOTICE,'connections':sorted(connections,key=lambda i:i['id']),'result':result}

    def context(self,private,state,now):
        allowed=set();ownership=[];successes=[];selected=[];inventory_ready=True
        for item in private['items'].values():
            if item['connected']:
                ownership.append({'item_id':item['item_id'],'generation':item['generation']})
            for account in item['accounts'].values():
                if item['connected'] and account['enabled'] and account['availability']=='available':
                    selected.append(account);inventory_ready &= item['status']=='connected'
                    stamp=account['stream']['last_sync_at']
                    if stamp:successes.append(stamp)
                    if account['stream']['status']=='ready' and stamp and instant(now)-instant(stamp)<=timedelta(days=1):
                        allowed.update(account['stream']['records'].values())
        ready=bool(selected) and inventory_ready and all(a['stream']['status']=='ready' for a in selected)
        if not ready:allowed.clear()
        excluded=[o['id'] for o in state['observations'] if o['source_id']=='plaid' and o['id'] not in allowed]
        return {'excluded_ids':sorted(set(excluded)),'items':ownership,'selected_count':len(selected),'ready':ready,
                'last_success_at':max(successes) if successes else None,'connected':bool(ownership)}

    async def handle(self,operation,payload):
        if operation=='digest':return {'digest':self.vault.digest(payload)}
        if operation=='receipt_seal':return {'ciphertext':self.vault.seal(payload)}
        if operation=='receipt_open':return self.vault.open(payload['ciphertext'])
        if operation=='committed':self.vault.clear_pending();return {'committed':True}
        if operation=='clear':self.vault.clear();return {'cleared':True}
        private=self.open(payload.get('vault'))
        state,now=payload.get('state'),payload.get('now')
        if operation=='project':
            self.live_versions={cid:i['generation'] for cid,i in private['items'].items() if i['connected']};self.live_epoch=state['data_epoch']
            return {'view':self.view(private,state),'context':self.context(private,state,now)}
        if operation=='describe':return {'items':{cid:{k:i[k] for k in ('item_id','generation','connected')} for cid,i in private['items'].items()},'sessions':deepcopy(private['sessions'])}
        if operation=='merge':
            body,outcome=payload['request'],payload['outcome'];action=body['action'];counts={'added':0,'modified':0,'removed':0};partial=False
            observations=deepcopy(state['observations'])
            def session_ok(sid):
                session=private['sessions'].get(sid)
                if not session or session['cancelled']:raise DomainError('obsolete_selection',409)
            if action in {'add_link','update_link'}:
                private['sessions'][outcome['session_id']]=outcome['session']
                private['sessions']={k:v for k,v in list(private['sessions'].items())[-32:]}
            elif action in {'exchange','reconcile'}:
                session=private['sessions'].get(outcome['session_id'])
                if action=='exchange' and session and session['cancelled']:
                    candidate=deepcopy(outcome['item']);candidate.update(connected=False,status='disconnected',removal_pending=True,generation=candidate['generation']+1)
                    for account in candidate['accounts'].values():account['enabled']=False;account['stream']['status']='disabled'
                    existing=next((i for i in private['items'].values() if i['item_id']==candidate['item_id']),None)
                    if existing is None:private['items'][candidate['id']]=candidate
                    private['sessions'].pop(outcome['session_id'],None)
                    return {'vault':self.vault.seal(private),'observations':observations,'counts':counts,'partial':True,'cancelled':True}
                session_ok(outcome['session_id'])
                candidate=outcome['item']
                if action=='exchange':
                    if any(i['item_id']==candidate['item_id'] for i in private['items'].values()):raise DomainError('obsolete_selection',409)
                else:
                    current=private['items'].get(candidate['id'])
                    if not current or current['generation']!=payload['captured'].get(candidate['id']):raise DomainError('obsolete_selection',409)
                private['items'][candidate['id']]=candidate
                private['sessions'].pop(outcome['session_id'],None)
                partial=candidate['status']=='attention'
            elif action=='sync':
                input_keys={tuple(v) for v in payload['input_keys']};present={(o['id'],o['revision']) for o in observations}
                from datetime import date
                from ..contracts import RULES
                cutoff=(date.fromisoformat(state['evaluation_date'])-timedelta(days=RULES['retention']['observation_days']-1)).isoformat()
                for result in outcome['outcomes']:
                    candidate=result['item'];current=private['items'].get(candidate['id'])
                    if not current or not current['connected'] or current['generation']!=payload['captured'].get(candidate['id']):partial=True;continue
                    private['items'][candidate['id']]=candidate;partial|=result['partial']
                    for o in result['observations']:
                        k=(o['id'],o['revision'])
                        if k not in input_keys and k not in present and o['local_date']>=cutoff:observations.append(o);present.add(k)
                    for k in counts:counts[k]+=result['counts'][k]
            return {'vault':self.vault.seal(private),'observations':observations,'counts':counts,'partial':partial}
        if operation=='prune':
            ids=set(payload['observation_ids'])
            for item in private['items'].values():
                for raw,app_id in list(item.get('legacy_records',{}).items()):
                    if app_id not in ids:item.setdefault('legacy_expired',[]).append(raw);del item['legacy_records'][raw]
                item['legacy_expired']=item.get('legacy_expired',[])[-10000:]
                # Keep provider->app IDs for retained/replay dedupe, but expired IDs
                # are fenced separately so backfill cannot revive erased history.
                for account in item['accounts'].values():
                    stream=account['stream']
                    for raw,app_id in list(stream['records'].items()):
                        if app_id not in ids:stream.setdefault('expired',[]).append(raw);del stream['records'][raw]
                    stream['expired']=stream.get('expired',[])[-10000:]
            return {'vault':self.vault.seal(private)}
        if operation=='finish_recovery':
            pending=self.vault.pending();captured=pending.get('connection') or pending.get('item',{})
            if captured and pending.get('epoch')==state['data_epoch'] and any(i['item_id']==captured.get('item_id') for i in private['items'].values()):self.vault.clear_pending()
            return {'recovered':True}
        if operation=='upgrade':
            legacy=self.vault.open(payload.get('legacy_vault'));pending=self.vault.pending()
            connected=bool(payload.get('legacy_connected')) and not legacy.get('removal_pending')
            if not legacy.get('access_token') and legacy.get('reservation') and legacy.get('epoch')==state['data_epoch'] and pending.get('epoch')==state['data_epoch'] and pending.get('connection',{}).get('access_token'):
                legacy=pending['connection'];connected=True
            if not legacy.get('access_token'):return {'vault':self.vault.seal(private)}
            if not isinstance(legacy.get('item_id'),str) or not 1<=len(legacy['item_id'])<=256:raise RuntimeError('Legacy connection has no verified Item identity')
            item={'id':str(uuid4()),'item_id':legacy['item_id'],'access_token':legacy['access_token'],'connected':connected,'generation':1,
                'status':'attention' if connected else 'disconnected','removal_pending':bool(legacy.get('removal_pending')),'accounts':{},'institution_name':None,'last_inventory_at':None,'last_sync_at':None,
                'legacy_records':deepcopy(legacy.get('records',{}))}
            # No old Item cursor becomes an account cursor. Legacy consent did not
            # identify individual accounts, so they need explicit selection.
            if connected:
                if self.claim_item is not None and not await self.claim_item(item['item_id'],state['data_epoch']):raise DomainError('consent_required',409)
                try:item=await self.inventory(item,now)
                except Exception:item['status']='attention'
            private['items'][item['id']]=item
            return {'vault':self.vault.seal(private)}
        if operation=='local':
            body=payload['request'];action=body['action'];item=private['items'].get(body.get('connection_id'))
            if action=='cancel_link':
                session=private['sessions'].get(body['link_session_id'])
                if not session:raise DomainError('not_found',404)
                session['cancelled']=True
                pending=self.vault.pending()
                if pending.get('banking_session')==body['link_session_id'] and pending.get('epoch')==state['data_epoch'] and pending.get('ownership_proven'):
                    candidate=deepcopy(pending['item']);candidate.update(connected=False,status='disconnected',removal_pending=True,generation=candidate['generation']+1)
                    for account in candidate['accounts'].values():account['enabled']=False;account['stream']['status']='disabled'
                    if not any(i['item_id']==candidate['item_id'] for i in private['items'].values()):private['items'][candidate['id']]=candidate
            else:
                if not item or not item['connected']:raise DomainError('not_found',404)
                selected=set(body['account_ids']);rows={a['id']:a for a in item['accounts'].values()}
                if not selected<=rows.keys() or any(rows[a]['availability']!='available' for a in selected):raise DomainError('obsolete_selection',409)
                if action=='select_group':
                    group=[a['id'] for a in item['accounts'].values() if group_id(a['type'],a['subtype'])==body['group_id'] and a['availability']=='available']
                    if selected!=set(group):raise DomainError('obsolete_selection',409)
                for app_id in selected:
                    a=rows[app_id]
                    if a['enabled']!=body['enabled']:
                        a['enabled']=body['enabled'];a['stream']['status']='not_synced' if body['enabled'] else 'disabled'
                item['generation']+=1
            return {'vault':self.vault.seal(private)}
        if operation=='revoke_all':
            captured=[]
            for item in private['items'].values():
                if item['connected']:
                    captured.append(deepcopy(item));item.update(connected=False,status='disconnected',generation=item['generation']+1,removal_pending=True)
                    for a in item['accounts'].values():a['enabled']=False;a['stream']['status']='disabled'
            for session in private['sessions'].values():session['cancelled']=True
            return {'vault':self.vault.seal(private),'items':captured}
        if operation=='disconnect_local':
            item=private['items'].get(payload['connection_id'])
            if not item:raise DomainError('not_found',404)
            captured=deepcopy(item)
            item.update(connected=False,status='disconnected',generation=item['generation']+1,removal_pending=True)
            for a in item['accounts'].values():a['enabled']=False;a['stream']['status']='disabled'
            return {'vault':self.vault.seal(private),'item':captured}
        if operation=='remove':return {'confirmed':await self.remove(payload['item'])}
        if operation=='disconnect_done':
            item=private['items'].get(payload['connection_id'])
            if item:
                item['removal_pending']=not payload['confirmed']
                if payload['confirmed']:item.pop('access_token',None)
            return {'vault':self.vault.seal(private)}
        if operation=='list_private':
            items=deepcopy(list(private['items'].values()));staged=self.vault.pending();pending=staged.get('item') or staged.get('connection')
            if pending and not any(i['item_id']==pending['item_id'] for i in items):items.append(pending)
            return {'items':items}
        if self.status!='available':raise DomainError('not_configured',409)
        body=payload['request'];action=body['action'];item=private['items'].get(body.get('connection_id'))
        if action in {'update_link','reconcile'} and (not item or not item['connected']):raise DomainError('not_found',404)
        if operation=='network' and action in {'add_link','update_link'}:
            request={'client_name':'Local Companion','language':'en','country_codes':['US'],'user':{'client_user_id':self.client_user_id or str(uuid4())}}
            if action=='add_link':request['products']=['transactions'];request['transactions']={'days_requested':90}
            else:request.update(access_token=item['access_token'],update={'account_selection_enabled':True})
            if self.webhook_url:request['webhook']=self.webhook_url
            data=await self.client.request('/link/token/create',request)
            if not isinstance(data.get('link_token'),str) or not data['link_token'].startswith('link-sandbox-') or not isinstance(data.get('expiration'),str):raise PlaidFailure()
            sid=str(uuid4());session={'kind':'add' if action=='add_link' else 'update','connection_id':body.get('connection_id'),'expiration':data['expiration'],'cancelled':False}
            return {'session_id':sid,'session':session,'link_token':data['link_token'],'expiration':data['expiration']}
        session=private['sessions'].get(body.get('link_session_id'))
        if action in {'exchange','reconcile'}:
            if not session or session['cancelled'] or instant(session['expiration'])<=instant(now):raise DomainError('obsolete_selection',409)
            if action=='exchange' and session['kind']!='add' or action=='reconcile' and (session['kind']!='update' or session['connection_id']!=body['connection_id']):raise DomainError('obsolete_selection',409)
        if operation=='network' and action=='exchange':
            pending=self.vault.pending()
            token_digest=self.vault.digest({'public_token':body['public_token']})
            if pending.get('banking_session')==body['link_session_id'] and pending.get('epoch')==state['data_epoch']:
                if pending.get('public_token_digest')!=token_digest:raise DomainError('idempotency_conflict',409)
                item=pending['item']
            elif pending:
                raise DomainError('busy',409,'A prior encrypted exchange requires reconciliation before adding another bank.')
            else:
                data=await self.client.request('/item/public_token/exchange',{'public_token':body['public_token']})
                if not isinstance(data.get('access_token'),str) or not data['access_token'].startswith('access-sandbox-') or not isinstance(data.get('item_id'),str):raise PlaidFailure()
                item={'id':str(uuid4()),'item_id':data['item_id'],'access_token':data['access_token'],'connected':True,'generation':1,'status':'connected',
                      'accounts':{},'institution_name':None,'last_inventory_at':None,'last_sync_at':None,'legacy_records':{}}
                self.vault.stage_exchange({'banking_session':body['link_session_id'],'epoch':state['data_epoch'],'item':item,'public_token_digest':token_digest})
            if any(i['item_id']==item['item_id'] for i in private['items'].values()):
                self.vault.clear_pending();raise DomainError('obsolete_selection',409,'This bank connection already exists; use Manage accounts.')
            if self.claim_item is not None and not await self.claim_item(item['item_id'],state['data_epoch']):
                self.vault.clear_pending();raise DomainError('consent_required',409,'Bank connection ownership could not be established.')
            self.vault.stage_exchange({'banking_session':body['link_session_id'],'epoch':state['data_epoch'],'item':item,'public_token_digest':token_digest,'ownership_proven':True})
            try:item=await self.inventory(item,now)
            except Exception:item['status']='attention'
            return {'item':item,'session_id':body['link_session_id']}
        if operation=='network' and action=='reconcile':return {'item':await self.inventory(item,now),'session_id':body['link_session_id']}
        if operation=='network' and action=='sync':
            items=[i for i in private['items'].values() if i['connected'] and (body['connection_id'] is None or i['id']==body['connection_id'])]
            if body['connection_id'] and not items:raise DomainError('not_found',404)
            outcomes=[]
            for original in items:
                candidate=deepcopy(original);observations=deepcopy(state['observations']);counts={'added':0,'modified':0,'removed':0};partial=False
                try:candidate=await self.inventory(candidate,now)
                except Exception:candidate['status']='attention';outcomes.append({'item':candidate,'observations':[],'counts':counts,'partial':True});continue
                def guard():
                    if self.live_epoch!=state['data_epoch'] or self.live_versions.get(original['id'])!=original['generation']:raise PlaidFailure()
                for raw,a in candidate['accounts'].items():
                    if not a['enabled'] or a['availability']!='available':continue
                    try:
                        staged=await self.client.sync_pages(candidate['access_token'],a['stream']['cursor'],guard=guard,account_id=raw)
                        if any(not isinstance(row,dict) or row.get('account_id')!=raw for field in ('added','modified','removed') for row in staged[field]):raise PlaidFailure()
                        expired=set(a['stream'].get('expired',[])) | set(candidate.get('legacy_expired',[]))
                        for field in ('added','modified'):staged[field]=[r for r in staged[field] if r['transaction_id'] not in expired]
                        mapping=deepcopy(a['stream']['records'])
                        # First filtered replay can prove ownership of retained legacy IDs.
                        other={v for other_raw,other in candidate['accounts'].items() if other_raw!=raw for v in other['stream']['records'].values()}
                        for row in staged['added']+staged['modified']:
                            for identifier in (row['transaction_id'],row.get('pending_transaction_id')):
                                app_id=candidate.get('legacy_records',{}).get(identifier)
                                if app_id and app_id not in other:mapping.setdefault(identifier,app_id)
                        projected=project({**state,'observations':observations},{'records':mapping},staged)
                        observations=projected['observations'];a['stream'].update(projected['connection'])
                        a['stream'].update(last_sync_at=now,status='ready' if staged['status']=='HISTORICAL_UPDATE_COMPLETE' else 'syncing')
                        partial|=a['stream']['status']!='ready'
                        for k in counts:counts[k]+=projected['counts'][k]
                    except Exception:a['stream']['status']='error';partial=True
                candidate.update(last_sync_at=now,status='attention' if partial else 'connected')
                outcomes.append({'item':candidate,'observations':observations,'counts':counts,'partial':partial})
            return {'outcomes':outcomes}
        raise ValueError('Unsupported banking operation')
