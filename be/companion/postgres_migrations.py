"""Reviewed explicit operator DDL. Importing this module never executes it."""
MIGRATION_V1 = r"""
BEGIN;
CREATE SCHEMA IF NOT EXISTS companion_private;
REVOKE ALL ON SCHEMA companion_private FROM PUBLIC;
CREATE TABLE IF NOT EXISTS companion_private.schema_version(version integer PRIMARY KEY CHECK(version=1));
INSERT INTO companion_private.schema_version(version) VALUES(1) ON CONFLICT(version) DO NOTHING;
CREATE TABLE IF NOT EXISTS companion_private.accounts (
 account_id uuid PRIMARY KEY, issuer text NOT NULL, subject text NOT NULL,
 generation bigint NOT NULL DEFAULT 0, UNIQUE(issuer,subject));
CREATE TABLE IF NOT EXISTS companion_private.snapshots (
 account_id uuid PRIMARY KEY REFERENCES companion_private.accounts(account_id), body jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS companion_private.receipts (
 account_id uuid NOT NULL REFERENCES companion_private.accounts(account_id), epoch text NOT NULL,
 route text NOT NULL, request_id text NOT NULL, digest text NOT NULL, response jsonb,
 created_at timestamptz NOT NULL, PRIMARY KEY(account_id,epoch,route,request_id));
CREATE TABLE IF NOT EXISTS companion_private.plaid_owners (
 environment text NOT NULL CHECK(environment='sandbox'), item_id text NOT NULL,
 account_id uuid NOT NULL REFERENCES companion_private.accounts(account_id),
 epoch text NOT NULL, generation bigint NOT NULL, active boolean NOT NULL,
 PRIMARY KEY(environment,item_id));
CREATE TABLE IF NOT EXISTS companion_private.webhook_receipts (
 digest text PRIMARY KEY CHECK(length(digest)=64), received_at timestamptz NOT NULL,
 environment text NOT NULL CHECK(environment='sandbox'), item_id text NOT NULL,
 expires_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS companion_private.webhook_jobs (
 environment text NOT NULL CHECK(environment='sandbox'), item_id text NOT NULL,
 account_id uuid REFERENCES companion_private.accounts(account_id), epoch text,
 owner_generation bigint, requested bigint NOT NULL DEFAULT 1, completed bigint NOT NULL DEFAULT 0,
 lease_token uuid, lease_until timestamptz, attempts integer NOT NULL DEFAULT 0,
 available_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 PRIMARY KEY(environment,item_id));
CREATE INDEX IF NOT EXISTS webhook_receipts_expiry ON companion_private.webhook_receipts(expires_at);
CREATE INDEX IF NOT EXISTS webhook_jobs_ready ON companion_private.webhook_jobs(available_at) WHERE completed<requested;
CREATE INDEX IF NOT EXISTS plaid_owner_account ON companion_private.plaid_owners(account_id);
CREATE OR REPLACE FUNCTION companion_private.immutable_item_owner() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.account_id<>OLD.account_id OR NEW.environment<>OLD.environment OR NEW.item_id<>OLD.item_id THEN
  RAISE EXCEPTION 'Item ownership is immutable';
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS immutable_item_owner ON companion_private.plaid_owners;
CREATE TRIGGER immutable_item_owner BEFORE UPDATE ON companion_private.plaid_owners
 FOR EACH ROW EXECUTE FUNCTION companion_private.immutable_item_owner();
ALTER TABLE companion_private.snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE companion_private.snapshots FORCE ROW LEVEL SECURITY;
ALTER TABLE companion_private.receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE companion_private.receipts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS account_scope ON companion_private.snapshots;
CREATE POLICY account_scope ON companion_private.snapshots
 USING(account_id::text=current_setting('companion.account_id',true))
 WITH CHECK(account_id::text=current_setting('companion.account_id',true));
DROP POLICY IF EXISTS account_scope ON companion_private.receipts;
CREATE POLICY account_scope ON companion_private.receipts
 USING(account_id::text=current_setting('companion.account_id',true))
 WITH CHECK(account_id::text=current_setting('companion.account_id',true));
REVOKE ALL ON ALL TABLES IN SCHEMA companion_private FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA companion_private FROM PUBLIC;
DO $$ DECLARE r text; BEGIN
 FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
   EXECUTE format('REVOKE ALL ON SCHEMA companion_private FROM %I',r);
   EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA companion_private FROM %I',r);
   EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA companion_private FROM %I',r);
  END IF;
 END LOOP;
END $$;
COMMIT;
"""
