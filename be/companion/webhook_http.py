"""Only signature-verified Plaid delivery bypasses Clerk; ingestion never syncs inline."""
import asyncio
from starlette.responses import JSONResponse


class SandboxVerificationKeys:
    def __init__(self,client): self.client=client
    async def __call__(self,kid):
        from .plaid_webhooks import WebhookError
        from .connections.plaid import PlaidFailure
        try:
            return await self.client.request('/webhook_verification_key/get',{'key_id':kid})
        except PlaidFailure as exc:
            raise WebhookError(401 if exc.code=='invalid_verification_key' else 503) from None


class WebhookReceiver:
    def __init__(self,verifier,inbox): self.verifier,self.inbox=verifier,inbox

    async def __call__(self,scope,receive,send):
        from .plaid_webhooks import WebhookError, MAX_BODY_BYTES
        status=503
        try:
            if scope.get('query_string'):status=400
            else:
                chunks,size=[],0
                async with asyncio.timeout(5):
                    while True:
                        message=await receive()
                        if message['type']=='http.disconnect':return
                        chunk=message.get('body',b'');size+=len(chunk)
                        if size>MAX_BODY_BYTES:
                            status=413
                            break
                        chunks.append(chunk)
                        if not message.get('more_body'):
                            event=await self.verifier.verify(b''.join(chunks),scope.get('headers',[]))
                            await self.inbox.enqueue_verified(event)
                            status=200
                            break
        except WebhookError as exc:status=exc.status
        except Exception:status=503
        response={'accepted':True} if status==200 else {'error':{'code':'webhook_unavailable' if status==503 else 'invalid_webhook','retryable':status==503}}
        await JSONResponse(response,status_code=status,headers={'Cache-Control':'no-store'})(scope,receive,send)
