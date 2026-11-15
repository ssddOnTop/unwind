"""Explicit notification composition, protected signing and authenticated self-account handlers."""
import asyncio,base64,json,logging,os,re,stat,time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID,uuid5,NAMESPACE_URL
import httpx
from cryptography.hazmat.primitives import serialization,hashes
from cryptography.hazmat.primitives.asymmetric import ec,utils
from .contracts import DomainError
from .report_notification_postgres import PostgresReportNotifications,PostgresNotificationWorker,HTTP2APNsProvider,NotificationError,session_key,permitted
from .report_notification_contracts import NotificationStatus,NotificationRegistration,NotificationLogoutResult
from .vault import PrivateVault

@dataclass(frozen=True,repr=False)
class NotificationConfig:
    enabled: bool=False
    apns_enabled: bool=False
    topic: str='unconfigured'
    environment: str='sandbox'
    team_id: str=''
    key_id: str=''
    signing_key_file: str=''
    @classmethod
    def from_env(cls):
        enabled=os.environ.get('COMPANION_REPORT_NOTIFICATIONS','disabled')
        apns=os.environ.get('COMPANION_APNS_ENABLED','disabled')
        environment=os.environ.get('COMPANION_APNS_ENVIRONMENT','sandbox')
        if enabled not in {'enabled','disabled'} or apns not in {'enabled','disabled'} or environment not in {'sandbox','production'}:raise ValueError('Invalid notification configuration')
        return cls(enabled=='enabled',apns=='enabled',os.environ.get('COMPANION_APNS_TOPIC','unconfigured'),environment,
                   os.environ.get('COMPANION_APNS_TEAM_ID',''),os.environ.get('COMPANION_APNS_KEY_ID',''),os.environ.get('COMPANION_APNS_SIGNING_KEY_FILE',''))

class APNsSigner:
    """ES256 provider JWT, cached for 40 minutes; no token/key representations or logs."""
    def __init__(self,config,clock=time.time):
        if not all(re.fullmatch(r'[A-Za-z0-9]{10}',v) for v in (config.team_id,config.key_id)):raise ValueError('APNs identifiers required')
        if config.topic=='unconfigured' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,254}',config.topic):raise ValueError('APNs topic required')
        path=Path(config.signing_key_file)
        if not path.is_absolute() or path.is_symlink():raise ValueError('Protected signing key required')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid!=os.getuid() or info.st_size>16384:raise ValueError('Unsafe signing key permissions')
            with os.fdopen(fd,'rb',closefd=False) as f:key=serialization.load_pem_private_key(f.read(),password=None)
        finally:os.close(fd)
        if not isinstance(key,ec.EllipticCurvePrivateKey) or not isinstance(key.curve,ec.SECP256R1):raise ValueError('APNs P-256 key required')
        self.key,self.config,self.clock=key,config,clock;self.cached=None;self.issued=0
    def __call__(self):
        now=int(self.clock())
        if self.cached and 0<=now-self.issued<2400:return self.cached
        def encode(value):return base64.urlsafe_b64encode(value).rstrip(b'=')
        header=encode(json.dumps({'alg':'ES256','kid':self.config.key_id},separators=(',',':')).encode())
        claims=encode(json.dumps({'iss':self.config.team_id,'iat':now},separators=(',',':')).encode())
        message=header+b'.'+claims
        r,s=utils.decode_dss_signature(self.key.sign(message,ec.ECDSA(hashes.SHA256())))
        self.cached=(message+b'.'+encode(r.to_bytes(32,'big')+s.to_bytes(32,'big'))).decode();self.issued=now
        return self.cached

class ReportNotificationService:
    def __init__(self,runtime,auth_root,config,*,provider=None,cipher=None):
        self.runtime,self.config=runtime,config
        self.queue=PostgresReportNotifications(runtime,cipher or PrivateVault(Path(auth_root)/'notification-vault'),topic=config.topic or 'unconfigured',environment=config.environment)
        self.worker=PostgresNotificationWorker(self.queue,provider)
        self.client=None;self.task=None;self.stop=asyncio.Event();self.ready=False
        self.provider_state='ready' if provider else 'disabled'
    async def available(self):
        if self.ready:return True
        await self.runtime.open()
        async with self.runtime.pool.connection() as c:
            exists=await (await c.execute("SELECT to_regclass('companion_private.report_notification_scopes') IS NOT NULL")).fetchone()
        if not exists[0]:return False
        await self.queue.check();self.ready=True;return True
    async def start(self,run_worker=True):
        if not self.config.enabled:return
        if not await self.available():raise RuntimeError('Explicit notification migration required')
        if self.worker.provider is None and self.config.apns_enabled:
            try:
                signer=APNsSigner(self.config)
                for name in ('httpx','httpcore','hpack','h2'):logging.getLogger(name).setLevel(logging.CRITICAL)
                self.client=httpx.AsyncClient(http2=True,trust_env=False,follow_redirects=False,timeout=10,limits=httpx.Limits(max_connections=4,max_keepalive_connections=2))
                self.worker.provider=HTTP2APNsProvider(self.client,signer);self.provider_state='ready'
            except Exception:self.provider_state='unavailable'
        if run_worker:self.task=asyncio.create_task(self.worker.run(self.stop))
    async def close(self):
        self.stop.set()
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True);self.task=None
        if self.client:await self.client.aclose();self.client=None
    async def status(self,principal,document,registration_request_id=None):
        status={'epoch':document['state']['data_epoch'],'revision':None,'eligible':permitted(document),'installations':[],'session_revoked':False}
        if await self.available():status=await self.queue.status(principal)
        selected=None
        if registration_request_id:
            account=await self.runtime.identity(principal)
            candidate=str(uuid5(NAMESPACE_URL,'report-installation:'+account+':'+str(UUID(registration_request_id))))
            if any(r['id']==candidate for r in status['installations']):selected=candidate
        delivery=self.provider_state if self.config.enabled else 'disabled'
        if self.worker.last_error or delivery=='ready' and self.worker.provider is None:delivery='unavailable'
        messages={'disabled':'Remote report alerts are not configured on this server. Nothing is sent.',
                  'unavailable':'Remote report delivery is unavailable. No successful delivery is claimed.',
                  'ready':'Optional generic alerts for current actionable lifestyle concerns. Unchanged concern sets are not repeated, and resolved or insufficient-data assessments do not send alerts. APNs acceptance does not guarantee delivery; report details stay private.'}
        if status.get('outcomes',{}).get('failed',0):messages[delivery]+=' Some alert requests failed; delivery is not confirmed.'
        if status['session_revoked']:messages[delivery]='Report alerts are revoked for this sign-in session. Sign in again before enabling them.'
        return NotificationStatus(data_epoch=status['epoch'],scope_revision=status['revision'],session_binding=session_key(principal),session_revoked=status['session_revoked'],selected_installation_id=selected,
            eligible=status['eligible'],delivery_status=delivery,title='Report-ready notifications',message=messages[delivery],
            enable_label='Enable report-ready alerts' if delivery=='ready' and status['eligible'] and not status['session_revoked'] else None,
            revoke_label='Turn off report-ready alerts',installations=status['installations'])
    async def register(self,principal,body,document):
        if not self.config.enabled or self.worker.provider is None or not await self.available():raise DomainError('not_configured',409,'Remote delivery is not configured.')
        try:
            result=await self.queue.register(principal,request_id=body.request_id,token=body.token,epoch=body.data_epoch,revision=body.scope_revision,
                installation_id=body.installation_id,expected_token_version=body.expected_token_version,expected_consent_revision=body.expected_consent_revision,opt_in=body.opt_in)
        except NotificationError:raise DomainError('invalid_request',409,'Notification admission changed. Refresh before trying again.') from None
        return NotificationRegistration(**result,status=await self.status(principal,document))
    async def revoke(self,principal,body,document):
        if await self.available():
            try:
                if body.installation_id:await self.queue.revoke(principal,body.installation_id,expected_consent_revision=body.expected_consent_revision)
                else:await self.queue.revoke_initial(principal,body.registration_request_id)
            except NotificationError:raise DomainError('invalid_request',409,'Notification admission changed.') from None
        return await self.status(principal,document,body.registration_request_id)
    async def logout(self,principal):
        if await self.available():
            try:await self.queue.logout(principal)
            except NotificationError as error:
                if str(error)!='account_snapshot_required':raise DomainError('invalid_request',409,'Notification logout could not be confirmed.') from None
        return NotificationLogoutResult(revoked=True)
