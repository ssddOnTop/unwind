"""Strict self-account notification API; no client topic, environment or identity selector."""
from typing import Literal
from uuid import UUID
from pydantic import Field,model_validator
from .patterns import Strict

class NotificationInstallation(Strict):
    id: str
    enabled: bool
    token_version: int
    consent_revision: int

class NotificationStatus(Strict):
    schema_version: Literal['1']='1'
    data_epoch: str
    scope_revision: int | None
    session_binding: str
    session_revoked: bool
    selected_installation_id: str | None
    eligible: bool
    delivery_status: Literal['disabled','unavailable','ready']
    title: str
    message: str
    enable_label: str | None
    revoke_label: str
    installations: list[NotificationInstallation]

class NotificationRegister(Strict):
    schema_version: Literal['1']
    request_id: str
    data_epoch: str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$')
    scope_revision: int=Field(gt=0)
    token: str=Field(repr=False,min_length=2,max_length=4096,pattern=r'^(?:[0-9a-fA-F]{2})+$')
    installation_id: str | None
    expected_token_version: int=Field(ge=0)
    expected_consent_revision: int | None
    opt_in: bool
    @model_validator(mode='after')
    def valid(self):
        UUID(self.request_id)
        if self.installation_id:UUID(self.installation_id)
        if self.expected_consent_revision is not None and self.expected_consent_revision<1:raise ValueError('Invalid consent revision')
        return self

class NotificationRegistration(Strict):
    schema_version: Literal['1']='1'
    installation_id: str
    token_version: int
    consent_revision: int
    status: NotificationStatus

class NotificationStatusRequest(Strict):
    schema_version: Literal['1']
    registration_request_id: str
    @model_validator(mode='after')
    def valid(self):UUID(self.registration_request_id);return self

class NotificationRevoke(Strict):
    schema_version: Literal['1']
    installation_id: str | None
    registration_request_id: str
    expected_consent_revision: int | None
    @model_validator(mode='after')
    def valid(self):
        UUID(self.registration_request_id)
        if self.installation_id:
            UUID(self.installation_id)
            if self.expected_consent_revision is None or self.expected_consent_revision<1:raise ValueError('Consent revision required')
        return self

class NotificationLogout(Strict):
    schema_version: Literal['1']
    request_id: str
    @model_validator(mode='after')
    def valid(self):UUID(self.request_id);return self

class NotificationLogoutResult(Strict):
    schema_version: Literal['1']='1'
    revoked: bool
