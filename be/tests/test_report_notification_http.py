"""Authenticated notification integration with restricted disposable PG and mocked APNs only."""
import asyncio,base64,json,os
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec,utils
from companion.auth_http import create_authenticated_app
from companion.bootstrap import UnavailableSynthesis
from companion.pattern_migrations import MIGRATION_V3
from companion.workflow_migrations import MIGRATION_V4
from companion.postgres import PostgresConfig,PostgresRuntime
from companion.report_notification_migrations import migrate
from companion.report_notification_service import NotificationConfig,APNsSigner
from companion.report_notification_contracts import NotificationStatus,NotificationRegistration
from companion.report_notifications import apns_payload
from test_postgres import local_pg_cluster,pg_dsn
from test_postgres_http import signing_key
from test_auth_http import Harness,NOW
from test_pattern_bridges import consent

class Provider:
    def __init__(self):self.calls=[]
    async def send(self,admission):self.calls.append(apns_payload(admission.job));return 'accepted',False

@pytest.fixture
async def http_app(tmp_path,pg_dsn,signing_key):
    role='notify_http_'+uuid4().hex
    with psycopg.connect(pg_dsn,autocommit=True) as c:
        c.execute(MIGRATION_V3);c.execute(MIGRATION_V4)
        c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA companion_private TO {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA companion_private TO {}').format(sql.Identifier(role)))
        migrate(c,role)
    runtime=PostgresRuntime(PostgresConfig(make_conninfo(pg_dsn,user=role),local_test=True));provider=Provider()
    h=Harness(tmp_path/'private',signing_key)
    h.app=create_authenticated_app(tmp_path/'private',verifier=h.verifier,clock=lambda:NOW,housekeeping_interval=3600,
        postgres_runtime=runtime,experience_profile=True,notification_config=NotificationConfig(enabled=True,topic='test.bundle'),
        notification_provider=provider,notification_worker_enabled=False,
        replacements_factory=lambda path:{'synthesis':UnavailableSynthesis(path/'checkpoints.sqlite')})
    return h,runtime,provider

async def eligible(client,h,who='a'):
    check=await client.get('/state',headers=h.headers(who));assert check.status_code==200,check.text
    state=await consent(client,h,who);prefs=deepcopy(state['preferences']);prefs['cloud_consent']=True
    r=await client.put('/preferences',headers=h.headers(who),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'preferences':prefs})
    assert r.status_code==200,r.text
    return r.json()

async def status(client,h,who='a'):
    r=await client.get('/report-notifications/v1',headers=h.headers(who));assert r.status_code==200,r.text
    NotificationStatus.model_validate(r.json());return r.json()

def register_body(s,**values):
    return dict(schema_version='1',request_id=str(uuid4()),data_epoch=s['data_epoch'],scope_revision=s['scope_revision'],token='ab12',
                installation_id=None,expected_token_version=0,expected_consent_revision=None,opt_in=True)|values

async def report(service,account):
    queue=service.queue;run=str(uuid4())
    async with queue.scoped(account) as c:
        await c.execute("INSERT INTO companion_private.pattern_runs(account_id,run_id,request_id,input_digest,input_version,status,engine,evidence,is_demo) VALUES(%s,%s,%s,%s,'offline','running','deterministic','{}',false)",(account,run,str(uuid4()),uuid4().hex*2))
        await c.execute("INSERT INTO companion_private.pattern_insights(account_id,run_id,concern_id,rank) VALUES(%s,%s,'activity',0)",(account,run))
        await c.execute("UPDATE companion_private.pattern_runs SET status='complete' WHERE account_id=%s AND run_id=%s",(account,run))
    return run

async def test_routes_auth_cas_owned_encryption_status_revoke_and_logout(http_app):
    h,runtime,provider=http_app
    async with h.client() as client:
        for method,path in [('GET','/report-notifications/v1')]+[('POST','/report-notifications/v1/'+p) for p in ('status','register','revoke','logout')]:
            assert (await client.request(method,path,json={} if method=='POST' else None)).status_code==401
        await eligible(client,h);s=await status(client,h);assert s['delivery_status']=='ready' and not s['installations']
        body=register_body(s)
        for forbidden,value in [('account_id',str(uuid4())),('topic','other.bundle'),('environment','production'),('due_at','tomorrow')]:
            assert (await client.post('/report-notifications/v1/register',headers=h.headers(),json=body|{forbidden:value})).status_code==422
        r=await client.post('/report-notifications/v1/register',headers=h.headers(),json=body);assert r.status_code==200,r.text
        registered=r.json();NotificationRegistration.model_validate(registered)
        assert body['token'] not in r.text
        assert (await client.post('/report-notifications/v1/register',headers=h.headers(),json=body)).json()==registered
        found=await client.post('/report-notifications/v1/status',headers=h.headers(),json={'schema_version':'1','registration_request_id':body['request_id']})
        assert found.json()['selected_installation_id']==registered['installation_id']
        await eligible(client,h,'b');assert not (await status(client,h,'b'))['installations']
        bad=register_body(await status(client,h,'b'),installation_id=registered['installation_id'],expected_token_version=registered['token_version'],expected_consent_revision=registered['consent_revision'])
        assert (await client.post('/report-notifications/v1/register',headers=h.headers('b'),json=bad)).status_code==409
        service=h.app.state.report_notifications;account=(await h.handle('a')).account_id
        async with service.queue.scoped(account) as c:
            cipher=(await (await c.execute('SELECT ciphertext FROM companion_private.report_notification_registrations')).fetchone())[0]
            assert body['token'] not in cipher and service.queue.cipher.open(cipher)=={'token':'ab12'}
        run=await report(service,account);await service.worker.tick();assert len(provider.calls)==1
        assert provider.calls[0]['aps']['alert']['body']=='A report is ready. Open the app to review it.'
        assert all(secret not in json.dumps(provider.calls) for secret in [account,run,body['token'],'activity'])
        await service.worker.tick();assert len(provider.calls)==1
        revoke={'schema_version':'1','installation_id':registered['installation_id'],'registration_request_id':body['request_id'],'expected_consent_revision':registered['consent_revision']}
        assert (await client.post('/report-notifications/v1/revoke',headers=h.headers(),json=revoke)).status_code==200
        assert not (await status(client,h))['installations'][0]['enabled']
        assert (await client.post('/report-notifications/v1/register',headers=h.headers(),json=body)).status_code==409
        logout=await client.post('/report-notifications/v1/logout',headers=h.headers(),json={'schema_version':'1','request_id':str(uuid4())})
        assert logout.json()['revoked'] is True
        assert (await status(client,h))['session_revoked']
        assert not (await status(client,h,'b'))['session_revoked']
        assert (await client.post('/report-notifications/v1/register',headers=h.headers(),json=register_body(await status(client,h)))).status_code==409

async def test_initial_upload_cancellation_disabled_provider_and_actual_reset(http_app):
    h,_,provider=http_app
    async with h.client() as client:
        state=await eligible(client,h);s=await status(client,h);body=register_body(s)
        cancel={'schema_version':'1','installation_id':None,'registration_request_id':body['request_id'],'expected_consent_revision':None}
        assert (await client.post('/report-notifications/v1/revoke',headers=h.headers(),json=cancel)).status_code==200
        assert (await client.post('/report-notifications/v1/register',headers=h.headers(),json=body)).status_code==409
        s=await status(client,h);old=s['installations'][0]
        request=register_body(s,installation_id=old['id'],expected_token_version=old['token_version'],expected_consent_revision=old['consent_revision'])
        r=await client.post('/report-notifications/v1/register',headers=h.headers(),json=request);assert r.status_code==200
        # Late initial cancellation cannot revoke the newer consent revision.
        await client.post('/report-notifications/v1/revoke',headers=h.headers(),json=cancel)
        assert (await status(client,h))['installations'][0]['enabled']
        service=h.app.state.report_notifications;account=(await h.handle('a')).account_id
        await report(service,account);service.worker.provider=None;await service.worker.tick()
        async with service.queue.scoped(account) as c:
            assert (await (await c.execute('SELECT attempts FROM companion_private.report_notification_outbox')).fetchone())[0]==0
        assert not provider.calls
        # Revocation and reset still work while delivery is disabled.
        r=await client.request('DELETE','/data',headers=h.headers(),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'confirm':'delete_all'})
        assert r.status_code==200,r.text
        async with service.queue.scoped(account) as c:
            for table in ('report_notification_registrations','report_notification_outbox','report_notification_events'):
                assert (await (await c.execute(sql.SQL('SELECT count(*) FROM companion_private.{}').format(sql.Identifier(table)))).fetchone())[0]==0
        assert (await status(client,h))['data_epoch']!=body['data_epoch']
        assert (await client.post('/report-notifications/v1/register',headers=h.headers(),json=body)).status_code==409

async def test_source_revocation_between_status_and_register_fails_closed(http_app):
    h,_,_=http_app
    async with h.client() as client:
        state=await eligible(client,h);s=await status(client,h);body=register_body(s)
        prefs=deepcopy(state['preferences']);prefs['cloud_consent']=False
        r=await client.put('/preferences',headers=h.headers(),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'preferences':prefs});assert r.status_code==200
        assert (await client.post('/report-notifications/v1/register',headers=h.headers(),json=body)).status_code==409
        assert not (await status(client,h))['eligible']


def test_signer_protected_file_es256_claims_cache_and_http2_dependency(tmp_path):
    import h2  # installed from the reviewed httpx[http2] lock, not a runtime fallback
    key=ec.generate_private_key(ec.SECP256R1());path=tmp_path/'synthetic.p8'
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));path.chmod(0o600)
    clock=[100000];config=NotificationConfig(True,True,'test.bundle','sandbox','ABCDEFGHIJ','0123456789',str(path))
    signer=APNsSigner(config,lambda:clock[0]);token=signer();assert signer()==token
    def decode(x):return base64.urlsafe_b64decode(x+'='*(-len(x)%4))
    a,b,c=token.split('.');assert json.loads(decode(a))=={'alg':'ES256','kid':'0123456789'}
    assert json.loads(decode(b))=={'iss':'ABCDEFGHIJ','iat':100000}
    sig=decode(c);key.public_key().verify(utils.encode_dss_signature(int.from_bytes(sig[:32],'big'),int.from_bytes(sig[32:],'big')),(a+'.'+b).encode(),ec.ECDSA(hashes.SHA256()))
    clock[0]+=2401;assert signer()!=token
    path.chmod(0o644)
    with pytest.raises(ValueError):APNsSigner(config)

async def test_concurrent_registration_and_reset_do_not_deadlock_or_resurrect_token(http_app):
    h,_,_=http_app
    async with h.client() as client:
        state=await eligible(client,h);body=register_body(await status(client,h))
        registration,deleted=await asyncio.gather(
            client.post('/report-notifications/v1/register',headers=h.headers(),json=body),
            client.request('DELETE','/data',headers=h.headers(),json={'schema_version':'1','request_id':str(uuid4()),'data_epoch':state['data_epoch'],'confirm':'delete_all'}))
        assert registration.status_code in {200,409},registration.text
        assert deleted.status_code==200,deleted.text
        assert not (await status(client,h))['installations']
        assert (await client.post('/report-notifications/v1/register',headers=h.headers(),json=body)).status_code==409


def test_notification_schema_matches_published_models():
    from pathlib import Path
    from companion.report_notification_contracts import NotificationStatus,NotificationRegister,NotificationRegistration,NotificationRevoke,NotificationLogout,NotificationLogoutResult,NotificationStatusRequest
    definitions={}
    for model in (NotificationStatus,NotificationRegister,NotificationRegistration,NotificationRevoke,NotificationLogout,NotificationLogoutResult,NotificationStatusRequest):
        schema=model.model_json_schema();definitions.update(schema.pop('$defs',{}));definitions[model.__name__]=schema
    published=json.loads((Path(__file__).resolve().parents[2]/'contracts/report_notifications.schema.json').read_text())
    assert definitions==published['$defs']
