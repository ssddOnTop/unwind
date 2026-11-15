"""Multi-bank contract/API tests. All provider calls use an offline MockTransport."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from companion.bootstrap import UnavailableSynthesis
from companion.banking_contracts import validate_banking,SCHEMA
from companion.connections.plaid import PlaidSandboxClient,SandboxCredentials
from companion.extensions.banking import BankingExtension
from companion.extensions.plaid import PlaidExtension
from companion.http import create_app
from companion.plaid_contracts import validate_profile
from companion.vault import PrivateVault
from test_backend import NOW
from test_plaid import transaction,page


class BankProvider:
    def __init__(self):
        self.calls=[];self.items={};self.responses={};self.fail=set();self.entered=None;self.release=None
        self.add('one');self.add('two')

    def add(self,key,accounts=None,item_id=None):
        if accounts is None:
            accounts=[{'account_id':'same-provider-account','name':'PRIVATE Checking '+key,'official_name':'Private official '+key,'mask':'0007','type':'depository','subtype':'checking'},
                      {'account_id':'savings','name':'PRIVATE Savings '+key,'official_name':None,'mask':None,'type':'depository','subtype':'savings'}]
        self.items['access-sandbox-'+key]={'item':{'item_id':item_id or 'private-item-'+key,'institution_name':'PRIVATE Bank '+key,'institution_id':None},'accounts':deepcopy(accounts)}

    async def handle(self,request):
        body=json.loads(request.content);endpoint=request.url.path
        self.calls.append((endpoint,deepcopy(body)))
        if endpoint in self.fail:return httpx.Response(503,json={})
        if endpoint=='/link/token/create':return httpx.Response(200,json={'link_token':'link-sandbox-private-test','expiration':'2026-11-14T18:00:00Z'})
        if endpoint=='/item/public_token/exchange':
            key=body['public_token'].removeprefix('public-sandbox-');token='access-sandbox-'+key
            return httpx.Response(200,json={'access_token':token,'item_id':self.items[token]['item']['item_id']})
        if endpoint=='/item/remove':return httpx.Response(200,json={'removed':True})
        token=body['access_token']
        assert token in self.items,'No unknown/cross-user token may be used'
        if endpoint=='/accounts/get':return httpx.Response(200,json=self.items[token])
        assert endpoint=='/transactions/sync'
        raw=body['options']['account_id']
        if self.entered:
            self.entered.set();await self.release.wait()
        if raw in self.fail:return httpx.Response(503,json={})
        value=self.responses.get((token,raw))
        if value is None:
            rows=[transaction('transaction-'+str(i),account_id=raw,date='2026-11-'+str(10+i),authorized_date='2026-11-'+str(10+i)) for i in (1,2,3)]
            value=page(rows,cursor='cursor-'+token+'-'+raw)
        return httpx.Response(200,json=value)

    def client(self):return PlaidSandboxClient(SandboxCredentials('offline','offline'),httpx.MockTransport(self.handle))


def app_for(path,provider=None,**options):
    provider=provider or BankProvider()
    return create_app(path,banking_profile=True,experience_profile=True,clock=lambda:NOW,housekeeping_interval=3600,
        replacements={'banking':BankingExtension(path,provider.client()),'synthesis':UnavailableSynthesis()},**options)


def banking(client):
    response=client.get('/banking/v1');assert response.status_code==200,response.text
    value=response.json();validate_banking('banking',value);return value


def action_body(client,action,**fields):
    current=banking(client)
    return {'schema_version':'1','request_id':str(uuid4()),'data_epoch':current['data_epoch'],'expected_revision':current['revision'],'action':action,**fields}


def act(client,action,**fields):
    response=client.post('/banking/v1/actions',json=action_body(client,action,**fields))
    assert response.status_code==200,response.text
    value=response.json();validate_banking('banking',value);return value


def add(client,key='one'):
    linked=act(client,'add_link',consent_sandbox=True)
    value=act(client,'exchange',link_session_id=linked['result']['link_session_id'],public_token='public-sandbox-'+key)
    return next(c for c in value['connections'] if c['institution_name']=='PRIVATE Bank '+key)


def reconcile(client,connection):
    token=act(client,'update_link',connection_id=connection['id'])
    return act(client,'reconcile',connection_id=connection['id'],link_session_id=token['result']['link_session_id'])


def sync(client,connection=None):return act(client,'sync',connection_id=connection['id'] if connection else None)


@pytest.fixture
def bank(tmp_path):
    provider=BankProvider();app=app_for(tmp_path,provider)
    with TestClient(app,base_url='http://127.0.0.1') as client:yield client,provider,app


def test_explicit_profile_contract_openapi_and_private_inventory(bank):
    client,provider,app=bank
    assert banking(client)['connections']==[]
    spec=client.get('/openapi.json').json()
    assert len(spec['paths'])==27 and '/banking/v1/actions' in spec['paths']
    operation=spec['paths']['/banking/v1/actions']['post']
    assert operation['requestBody']['content']['application/json']['schema']['discriminator']['propertyName']=='action'
    assert '/banking/v1/demo' not in spec['paths']
    connection=add(client)
    assert all(a['enabled'] for a in connection['accounts'])
    assert next(a for a in connection['accounts'] if a['subtype']=='checking')['mask']=='0007'
    value=banking(client);raw=json.dumps(value)
    assert 'access-sandbox-' not in raw and 'private-item-' not in raw and 'same-provider-account' not in raw
    state=client.get('/state').json();validate_profile('state',state)
    assert not any(word in json.dumps(state) for word in ('PRIVATE Bank','PRIVATE Checking','0007','private-item-','same-provider-account'))
    assert not any(path=='/transactions/sync' for path,_ in provider.calls)
    create=next(body for path,body in provider.calls if path=='/link/token/create')
    assert create['products']==['transactions']


def test_multiple_items_identical_provider_account_and_transaction_ids_are_isolated(bank):
    client,provider,_=bank
    one,two=add(client),add(client,'two')
    assert one['id']!=two['id'] and not {a['id'] for a in one['accounts']} & {a['id'] for a in two['accounts']}
    value=sync(client)
    assert value['result']['added']==12
    state=client.get('/state').json();assert len(state['observations'])==12
    assert len({o['id'] for o in state['observations']})==12
    filtered=[body for path,body in provider.calls if path=='/transactions/sync']
    assert len(filtered)==4 and all('cursor' not in body for body in filtered)
    assert {(b['access_token'],b['options']['account_id']) for b in filtered}=={(t,a) for t in provider.items for a in ('same-provider-account','savings')}
    assert sync(client)['result']['added']==0
    assert len(client.get('/state').json()['observations'])==12
    assert all('cursor' in body for path,body in provider.calls[-6:] if path=='/transactions/sync')


def test_provider_deselection_keeps_immutable_history_but_excludes_all_new_insights(bank):
    client,provider,app=bank
    connection=add(client);sync(client)
    before=client.get('/state').json();assert len(before['observations'])==6
    provider.items['access-sandbox-one']['accounts']=[]
    value=reconcile(client,connection)
    after=client.get('/state').json();assert after['observations']==before['observations']
    assert all(o['operation']=='upsert' for o in after['observations'])
    assert all(not card['evidence'] for card in after['ledger'])
    assert not after['notification_intents']
    assert 'Plaid' in value['selection_notice'] and 'history' in value['selection_notice'].lower()
    experience=client.get('/experience/v1').json();assert experience['clarification'] is None
    assert all(not a['enabled'] for a in value['connections'][0]['accounts'])
    count=sum(path=='/transactions/sync' for path,_ in provider.calls);sync(client)
    assert sum(path=='/transactions/sync' for path,_ in provider.calls)==count
    assert client.get('/state').json()['observations']==before['observations']
    assert not any(path=='/item/remove' for path,_ in provider.calls)


def test_reenable_resumes_own_stream_without_duplicate_history(bank):
    client,provider,_=bank
    connection=add(client);sync(client)
    old=deepcopy(client.get('/state').json()['observations'])
    shared=deepcopy(provider.items['access-sandbox-one']['accounts'])
    provider.items['access-sandbox-one']['accounts']=[];reconcile(client,connection)
    provider.items['access-sandbox-one']['accounts']=shared;reconcile(client,connection)
    assert all(not c['evidence'] for c in client.get('/state').json()['ledger'])
    result=sync(client);assert result['result']['added']==0
    assert client.get('/state').json()['observations']==old
    assert all(body.get('cursor') for path,body in provider.calls[-3:] if path=='/transactions/sync')
    assert client.get('/state').json()['ledger'][0]['purchase_count']==6


def test_new_provider_shared_accounts_are_enabled_and_sync_without_local_selection(bank):
    client,provider,_=bank
    connection=add(client);sync(client)
    row=deepcopy(provider.items['access-sandbox-one']['accounts'][0]);row.update(account_id='new-checking',name='New private checking')
    provider.items['access-sandbox-one']['accounts'].append(row)
    value=reconcile(client,connection)
    assert len(value['connections'][0]['accounts'])==3
    assert all(a['enabled'] for a in value['connections'][0]['accounts'])
    assert sync(client)['result']['added']==3
    calls=[b for p,b in provider.calls if p=='/transactions/sync'][-3:]
    assert next(b for b in calls if b['options']['account_id']=='new-checking').get('cursor') is None
    assert all(b.get('cursor') for b in calls if b['options']['account_id']!='new-checking')


@pytest.mark.parametrize('action',['select_accounts','select_group'])
def test_app_selection_actions_are_removed_and_cannot_disable_shared_accounts(bank,action):
    client,provider,_=bank;connection=add(client);before=banking(client);calls=len(provider.calls)
    fields=dict(connection_id=connection['id'],account_ids=[a['id'] for a in connection['accounts']],enabled=False)
    if action=='select_group':fields['group_id']=connection['groups'][0]['id']
    response=client.post('/banking/v1/actions',json=action_body(client,action,**fields))
    assert response.status_code==422
    assert banking(client)==before and len(provider.calls)==calls


def test_update_link_uses_existing_token_and_reconcile_not_exchange(bank):
    client,provider,_=bank
    connection=add(client)
    token=act(client,'update_link',connection_id=connection['id']);sid=token['result']['link_session_id']
    request=next(body for path,body in reversed(provider.calls) if path=='/link/token/create')
    assert request['access_token']=='access-sandbox-one' and request['update']=={'account_selection_enabled':True}
    assert 'products' not in request
    count=sum(path=='/item/public_token/exchange' for path,_ in provider.calls)
    value=act(client,'reconcile',connection_id=connection['id'],link_session_id=sid)
    assert value['connections'][0]['id']==connection['id']
    assert sum(path=='/item/public_token/exchange' for path,_ in provider.calls)==count
    assert all(a['enabled'] for a in value['connections'][0]['accounts'])


def test_update_cancel_preserves_inventory_selection_and_does_not_reconcile(bank):
    client,provider,_=bank
    connection=add(client);before=banking(client)['connections']
    token=act(client,'update_link',connection_id=connection['id']);sid=token['result']['link_session_id']
    act(client,'cancel_link',link_session_id=sid)
    assert banking(client)['connections']==before
    count=len(provider.calls)
    body=action_body(client,'reconcile',connection_id=connection['id'],link_session_id=sid)
    assert client.post('/banking/v1/actions',json=body).status_code==409
    assert len(provider.calls)==count


def test_missing_accounts_and_unknown_inventory_fail_closed_without_losing_history(bank):
    client,provider,_=bank
    connection=add(client);sync(client)
    old=client.get('/state').json()['observations']
    provider.items['access-sandbox-one']['accounts']=[]
    sync(client)
    accounts=banking(client)['connections'][0]['accounts']
    assert all(a['availability']=='missing' and not a['enabled'] for a in accounts)
    assert client.get('/state').json()['observations']==old
    assert all(not c['evidence'] for c in client.get('/state').json()['ledger'])
    provider.items['access-sandbox-one']['accounts']=[{'account_id':'new-id','type':'future_kind','subtype':'future_subtype'}]
    result=sync(client);new=next(a for a in result['connections'][0]['accounts'] if a['availability']=='available')
    assert new['name']=='Unnamed account' and new['official_name'] is None and new['mask'] is None
    assert new['enabled'] and new['type']=='future_kind'


def test_partial_import_failure_preserves_other_stream_cursor_and_history(bank):
    client,provider,_=bank
    connection=add(client);provider.fail.add('savings')
    value=sync(client)
    assert value['result']['status']=='partial' and value['result']['added']==3
    assert len(client.get('/state').json()['observations'])==3
    assert all(not c['evidence'] for c in client.get('/state').json()['ledger'])
    statuses={a['subtype']:a['import_status'] for a in value['connections'][0]['accounts']}
    assert statuses=={'checking':'ready','savings':'error'}
    provider.fail.clear();value=sync(client)
    assert value['result']['added']==3 and len(client.get('/state').json()['observations'])==6
    requests=[b for p,b in provider.calls if p=='/transactions/sync'][-2:]
    assert next(b for b in requests if b['options']['account_id']=='savings').get('cursor') is None
    assert next(b for b in requests if b['options']['account_id']=='same-provider-account').get('cursor')


def test_disconnect_one_item_only_never_closes_account_and_keeps_other_bank(bank):
    client,provider,_=bank
    one,two=add(client),add(client,'two');sync(client)
    old=client.get('/state').json()['observations']
    value=act(client,'disconnect',connection_id=one['id'])
    assert value['result']['status']=='disconnected'
    assert client.get('/state').json()['observations']==old
    connections={c['id']:c for c in value['connections']}
    assert connections[one['id']]['status']=='disconnected' and connections[two['id']]['status']=='connected'
    removes=[b for p,b in provider.calls if p=='/item/remove'];assert removes==[{'access_token':'access-sandbox-one','client_id':'offline','secret':'offline'}]
    assert client.get('/state').json()['ledger'][0]['purchase_count']==6
    sync(client,two)
    assert not any(p in ('/accounts/close','/accounts/remove') for p,_ in provider.calls)


def test_disconnect_failure_is_local_disabled_and_explicit_remote_pending(bank):
    client,provider,_=bank
    connection=add(client);provider.fail.add('/item/remove')
    value=act(client,'disconnect',connection_id=connection['id'])
    assert value['result']['status']=='removal_pending'
    assert value['connections'][0]['status']=='disconnected' and value['connections'][0]['can_disconnect']
    provider.fail.clear();assert act(client,'disconnect',connection_id=connection['id'])['result']['status']=='disconnected'


@pytest.mark.parametrize('mutation',[{'expected_revision':999},{'account_ids':['unknown-account']},{'schema_version':'2'},{'user_id':'forged-user'},{'enabled':'yes'}])
def test_bad_or_stale_sync_rejected_atomically(bank,mutation):
    client,_,_=bank;connection=add(client)
    body=action_body(client,'sync',connection_id=connection['id'])
    before=client.get('/state').json();body.update(mutation)
    response=client.post('/banking/v1/actions',json=body)
    assert response.status_code in (409,422)
    assert client.get('/state').json()==before
    assert all(a['enabled'] for a in banking(client)['connections'][0]['accounts'])


def test_malformed_json_duplicate_keys_openapi_and_query_rejection(bank):
    client,_,_=bank
    for raw in ('{','{"action":"sync","action":"disconnect"}','null','[]'):
        response=client.post('/banking/v1/actions',content=raw,headers={'content-type':'application/json'})
        assert response.status_code in (400,422)
        validate_banking('error',response.json())
    assert client.get('/banking/v1?user_id=forged').status_code==422
    assert client.get('/banking/v1',headers={'Origin':'https://arbitrary.invalid'}).headers.get('access-control-allow-origin') is None


def test_receipts_exact_replay_conflict_and_encrypted_storage(bank,tmp_path):
    client,provider,_=bank;connection=add(client)
    body=action_body(client,'sync',connection_id=connection['id'])
    first=client.post('/banking/v1/actions',json=body);assert first.status_code==200
    assert client.post('/banking/v1/actions',json=body).json()==first.json()
    changed=body|{'connection_id':None}
    assert client.post('/banking/v1/actions',json=changed).json()['error']['code']=='idempotency_conflict'
    snapshot=(tmp_path/'state.sqlite').read_bytes()
    for secret in (b'PRIVATE Bank',b'PRIVATE Checking',b'access-sandbox-one',b'public-sandbox-one',b'"mask":"0007"'):
        assert secret not in snapshot


def test_restart_preserves_shared_inventory_and_receipts(tmp_path):
    provider=BankProvider()
    with TestClient(app_for(tmp_path,provider),base_url='http://127.0.0.1') as client:
        connection=add(client);sync(client)
        body=action_body(client,'sync',connection_id=connection['id'])
        reply=client.post('/banking/v1/actions',json=body).json();before=client.get('/state').json()['observations']
    with TestClient(app_for(tmp_path,provider),base_url='http://127.0.0.1') as client:
        assert client.post('/banking/v1/actions',json=body).json()==reply
        assert client.get('/state').json()['observations']==before
        assert sum(a['enabled'] for a in banking(client)['connections'][0]['accounts'])==2


def test_whole_data_delete_erases_all_banks_keys_and_epoch_and_stale_receipts(bank,tmp_path):
    client,provider,_=bank;one,two=add(client),add(client,'two');sync(client)
    prior=banking(client);body=action_body(client,'sync',connection_id=two['id'])
    response=client.request('DELETE','/data',json={'schema_version':'1','request_id':'delete-all','data_epoch':prior['data_epoch'],'confirm':'delete_all'})
    assert response.status_code==200,response.text
    assert banking(client)['data_epoch']!=prior['data_epoch'] and banking(client)['connections']==[]
    assert not client.get('/state').json()['observations']
    assert client.post('/banking/v1/actions',json=body).json()['error']['code']=='epoch_mismatch'
    removed=[b['access_token'] for p,b in provider.calls if p=='/item/remove'];assert set(removed)=={'access-sandbox-one','access-sandbox-two'}


@pytest.mark.asyncio
async def test_disconnect_during_sync_discards_late_pages_and_preserves_other_history(tmp_path):
    provider=BankProvider();app=app_for(tmp_path,provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
            async def request(action,**fields):
                state=(await client.get('/banking/v1')).json()
                return await client.post('/banking/v1/actions',json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'expected_revision':state['revision'],'action':action,**fields})
            linked=(await request('add_link',consent_sandbox=True)).json()
            value=(await request('exchange',link_session_id=linked['result']['link_session_id'],public_token='public-sandbox-one')).json()
            connection=value['connections'][0]
            provider.entered,provider.release=asyncio.Event(),asyncio.Event()
            pending=asyncio.create_task(request('sync',connection_id=connection['id']))
            await asyncio.wait_for(provider.entered.wait(),2)
            disabled=await request('disconnect',connection_id=connection['id'])
            assert disabled.status_code==200
            provider.release.set();response=await pending
            assert response.status_code==200,response.text
            assert response.json()['result']['status']=='partial'
            assert not (await client.get('/state')).json()['observations']
            assert sum(p=='/transactions/sync' for p,_ in provider.calls)==1


def test_examples_match_frozen_schema():
    for value in SCHEMA['examples']:validate_banking('banking',value)


@pytest.mark.parametrize('failure',['unavailable','duplicate','wrong_item','missing_accounts'])
def test_existing_app_disabled_accounts_migrate_only_after_successful_shared_inventory(bank,failure):
    client,provider,app=bank
    connection=add(client);sync(client)
    service=app.state.service;coordinator=service.banking_coordinator
    # Offline persisted old-format state: no provider/local live database is involved.
    async def old_selection():
        async with service.lock:
            doc=deepcopy(service.document)
            private=await coordinator.call('receipt_open',{'ciphertext':doc['meta']['banking_vault']})
            item=private['items'][connection['id']]
            for account in item['accounts'].values():
                account['enabled']=False;account['stream']['status']='disabled'
            doc['meta']['banking_vault']=(await coordinator.call('receipt_seal',private))['ciphertext']
            await service.evaluate(doc);await service.commit(doc)
            return deepcopy(item)
    old=client.portal.call(old_selection)
    cached=banking(client)['connections'][0]['accounts']
    assert not any(a['enabled'] for a in cached)
    history=client.get('/state').json()['observations']
    original=deepcopy(provider.items['access-sandbox-one'])
    if failure=='unavailable':provider.fail.add('/accounts/get')
    elif failure=='duplicate':provider.items['access-sandbox-one']['accounts'].append(deepcopy(original['accounts'][0]))
    elif failure=='wrong_item':provider.items['access-sandbox-one']['item']['item_id']='wrong-item'
    else:provider.items['access-sandbox-one'].pop('accounts')
    calls=sum(p=='/transactions/sync' for p,_ in provider.calls)
    value=sync(client)
    assert value['result']['status']=='partial'
    assert value['connections'][0]['accounts']==cached
    assert sum(p=='/transactions/sync' for p,_ in provider.calls)==calls
    assert client.get('/state').json()['observations']==history
    provider.fail.clear();provider.items['access-sandbox-one']=original
    value=sync(client)
    assert all(a['enabled'] for a in value['connections'][0]['accounts'])
    assert value['result']['added']==0 and client.get('/state').json()['observations']==history
    last=[b for p,b in provider.calls if p=='/transactions/sync'][-2:]
    assert all(b['cursor']==old['accounts'][b['options']['account_id']]['stream']['cursor'] for b in last)
    assert {a['id'] for a in value['connections'][0]['accounts']}=={a['id'] for a in cached}


def test_failed_reconcile_preserves_shared_inventory_history_and_can_retry(bank):
    client,provider,_=bank;connection=add(client);sync(client)
    before=banking(client)['connections'][0]['accounts'];history=client.get('/state').json()['observations']
    token=act(client,'update_link',connection_id=connection['id'])
    provider.fail.add('/accounts/get')
    body=action_body(client,'reconcile',connection_id=connection['id'],link_session_id=token['result']['link_session_id'])
    response=client.post('/banking/v1/actions',json=body)
    assert response.status_code==500
    assert banking(client)['connections'][0]['accounts']==before
    assert client.get('/state').json()['observations']==history
    provider.fail.clear()
    assert all(a['enabled'] for a in act(client,'reconcile',connection_id=connection['id'],link_session_id=token['result']['link_session_id'])['connections'][0]['accounts'])


@pytest.mark.parametrize('subtypes',[['checking'],['savings'],['checking','savings']])
def test_add_enables_exactly_provider_shared_checking_savings_or_both(bank,subtypes):
    client,provider,_=bank
    provider.items['access-sandbox-one']['accounts']=[a for a in provider.items['access-sandbox-one']['accounts'] if a['subtype'] in subtypes]
    connection=add(client)
    assert {a['subtype'] for a in connection['accounts']}==set(subtypes)
    assert all(a['enabled'] and a['import_status']=='not_synced' for a in connection['accounts'])
    assert sync(client)['result']['added']==3*len(subtypes)
    raw_ids={a['account_id'] for a in provider.items['access-sandbox-one']['accounts']}
    assert {b['options']['account_id'] for p,b in provider.calls if p=='/transactions/sync'}==raw_ids
