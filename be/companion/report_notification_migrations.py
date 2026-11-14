"""Additive, explicitly invoked notification migration. No startup DDL or DSN reads.

Independent of the shared schema_version number: workflow v4 still expects 4.
Run migrate() as the schema operator during the coordinated migration, not from
an application connection. Report events and snapshot invalidation are committed
in the SAME transaction as their source writes; no workflow callback is required.
"""
from psycopg import sql

TABLES = ('report_notification_scopes', 'report_notification_sessions',
          'report_notification_registrations', 'report_notification_events',
          'report_notification_outbox')
FUNCTIONS = ('report_notification_terminal', 'report_notification_invalidate')

DDL = r"""
LOCK TABLE companion_private.schema_version IN EXCLUSIVE MODE;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM companion_private.schema_version WHERE version IN (3,4)) THEN
  RAISE EXCEPTION 'Notifications require reviewed pattern schema 3 or workflow schema 4';
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS companion_private.report_notification_scopes (
 account_id uuid PRIMARY KEY REFERENCES companion_private.accounts(account_id),
 epoch text NOT NULL, revision bigint NOT NULL DEFAULT 1 CHECK(revision>0));
CREATE TABLE IF NOT EXISTS companion_private.report_notification_sessions (
 account_id uuid NOT NULL REFERENCES companion_private.accounts(account_id),
 session_key text NOT NULL CHECK(length(session_key)=64), revoked boolean NOT NULL DEFAULT false,
 PRIMARY KEY(account_id,session_key));
CREATE TABLE IF NOT EXISTS companion_private.report_notification_registrations (
 account_id uuid NOT NULL REFERENCES companion_private.accounts(account_id),
 installation_id uuid NOT NULL, session_key text NOT NULL,
 epoch text NOT NULL, scope_revision bigint NOT NULL, consent_revision bigint NOT NULL DEFAULT 1,
 token_version bigint NOT NULL DEFAULT 1, ciphertext text NOT NULL,
 topic text NOT NULL, environment text NOT NULL CHECK(environment IN ('sandbox','production')),
 enabled boolean NOT NULL DEFAULT true, opted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(account_id,installation_id),
 FOREIGN KEY(account_id,session_key) REFERENCES companion_private.report_notification_sessions(account_id,session_key),
 CHECK(scope_revision>0 AND consent_revision>0 AND token_version>0));
CREATE TABLE IF NOT EXISTS companion_private.report_notification_events (
 account_id uuid NOT NULL, epoch text NOT NULL, run_id uuid NOT NULL,
 completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(account_id,epoch,run_id),
 FOREIGN KEY(account_id,run_id) REFERENCES companion_private.pattern_runs(account_id,run_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS companion_private.report_notification_outbox (
 account_id uuid NOT NULL, notification_id uuid NOT NULL DEFAULT gen_random_uuid(),
 epoch text NOT NULL, run_id uuid NOT NULL, installation_id uuid NOT NULL,
 scope_revision bigint NOT NULL, consent_revision bigint NOT NULL,
 due_at timestamptz NOT NULL DEFAULT clock_timestamp(), available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','accepted','cancelled','failed')),
 attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5),
 lease_token uuid, lease_until timestamptz,
 PRIMARY KEY(account_id,notification_id), UNIQUE(account_id,epoch,run_id,installation_id),
 FOREIGN KEY(account_id,epoch,run_id) REFERENCES companion_private.report_notification_events(account_id,epoch,run_id) ON DELETE CASCADE,
 FOREIGN KEY(account_id,installation_id) REFERENCES companion_private.report_notification_registrations(account_id,installation_id),
 CHECK((status='sending') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)));
CREATE INDEX IF NOT EXISTS report_notification_outbox_due
 ON companion_private.report_notification_outbox(account_id,available_at) WHERE status='pending';
CREATE INDEX IF NOT EXISTS report_notification_events_time
 ON companion_private.report_notification_events(account_id,epoch,completed_at);

-- Invoker rights preserve existing account RLS. This trigger does NOT copy report content.
-- Insight rows may change later in the same report transaction; reconciliation checks
-- their committed content before enqueue and again immediately before sending.
CREATE OR REPLACE FUNCTION companion_private.report_notification_terminal()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE current_epoch text;
BEGIN
 IF NEW.status<>'complete' THEN RETURN NEW; END IF;
 IF TG_OP='UPDATE' THEN
  IF OLD.status='complete' THEN RETURN NEW; END IF;
 END IF;
 SELECT body->'state'->>'data_epoch' INTO current_epoch
 FROM companion_private.snapshots WHERE account_id=NEW.account_id;
 IF current_epoch IS NOT NULL THEN
  INSERT INTO companion_private.report_notification_events(account_id,epoch,run_id)
   VALUES(NEW.account_id,current_epoch,NEW.run_id) ON CONFLICT DO NOTHING;
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS report_notification_terminal ON companion_private.pattern_runs;
CREATE TRIGGER report_notification_terminal AFTER INSERT OR UPDATE OF status
 ON companion_private.pattern_runs FOR EACH ROW EXECUTE FUNCTION companion_private.report_notification_terminal();

-- Even a revoke/regrant between two worker polls invalidates earlier admission.
-- Snapshot reset and account-source consent writes already share one transaction.
CREATE OR REPLACE FUNCTION companion_private.report_notification_invalidate()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF (OLD.body->'state'->'data_epoch',OLD.body->'state'->'preferences'->'cloud_consent',
     (SELECT jsonb_agg(jsonb_build_array(s->'id',s->'enabled',s->'consent',s->'read_access') ORDER BY s->>'id')
      FROM jsonb_array_elements(COALESCE(OLD.body->'state'->'sources','[]'::jsonb)) s),
     OLD.body->'meta'->'banking_item_contexts')
 IS DISTINCT FROM
    (NEW.body->'state'->'data_epoch',NEW.body->'state'->'preferences'->'cloud_consent',
     (SELECT jsonb_agg(jsonb_build_array(s->'id',s->'enabled',s->'consent',s->'read_access') ORDER BY s->>'id')
      FROM jsonb_array_elements(COALESCE(NEW.body->'state'->'sources','[]'::jsonb)) s),
     NEW.body->'meta'->'banking_item_contexts') THEN
  UPDATE companion_private.report_notification_scopes SET
   epoch=COALESCE(NEW.body->'state'->>'data_epoch',''),revision=revision+1 WHERE account_id=NEW.account_id;
  UPDATE companion_private.report_notification_registrations SET
   enabled=false,ciphertext='',consent_revision=consent_revision+1 WHERE account_id=NEW.account_id;
  UPDATE companion_private.report_notification_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL
   WHERE account_id=NEW.account_id AND status IN ('pending','sending');
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS report_notification_invalidate ON companion_private.snapshots;
CREATE TRIGGER report_notification_invalidate AFTER UPDATE OF body ON companion_private.snapshots
 FOR EACH ROW EXECUTE FUNCTION companion_private.report_notification_invalidate();
"""


def migrate(connection, runtime_role: str) -> None:
    """Explicit operator call, all DDL/grants atomic; never opens a connection.

    Does not grant access to source tables: the existing runtime role must already
    have its reviewed account/snapshot/pattern grants. No PUBLIC/client-role access.
    Uses FORCE RLS on every new account-owned table, including token ciphertext.
    """
    if runtime_role in {'PUBLIC', 'postgres', 'anon', 'authenticated'}:
        raise ValueError('A dedicated restricted runtime role is required')
    with connection.transaction():
        row = connection.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s', (runtime_role,)).fetchone()
        owner = connection.execute('SELECT current_user').fetchone()[0]
        if row is None or any(row) or runtime_role == owner:
            raise ValueError('Runtime must not be the migration owner or bypass RLS')
        connection.execute(DDL)
        roles = [r[0] for r in connection.execute("SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated')")]
        for table in TABLES:
            name = sql.Identifier('companion_private', table)
            connection.execute(sql.SQL('ALTER TABLE {} ENABLE ROW LEVEL SECURITY').format(name))
            connection.execute(sql.SQL('ALTER TABLE {} FORCE ROW LEVEL SECURITY').format(name))
            connection.execute(sql.SQL('DROP POLICY IF EXISTS account_scope ON {}').format(name))
            connection.execute(sql.SQL("CREATE POLICY account_scope ON {} USING(account_id::text=current_setting('companion.account_id',true)) WITH CHECK(account_id::text=current_setting('companion.account_id',true))").format(name))
            connection.execute(sql.SQL('REVOKE ALL ON {} FROM PUBLIC').format(name))
            for role in roles:
                connection.execute(sql.SQL('REVOKE ALL ON {} FROM {}').format(name, sql.Identifier(role)))
            connection.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON {} TO {}').format(name, sql.Identifier(runtime_role)))
        for function in FUNCTIONS:
            name = sql.Identifier('companion_private', function)
            connection.execute(sql.SQL('REVOKE ALL ON FUNCTION {}() FROM PUBLIC').format(name))
            for role in roles:
                connection.execute(sql.SQL('REVOKE ALL ON FUNCTION {}() FROM {}').format(name, sql.Identifier(role)))
            connection.execute(sql.SQL('GRANT EXECUTE ON FUNCTION {}() TO {}').format(name, sql.Identifier(runtime_role)))


def main():
    import argparse,os,psycopg
    from .postgres import PostgresConfig
    parser=argparse.ArgumentParser(description='Explicit private report-notification migration; no startup DDL')
    parser.add_argument('--confirm-report-notification-schema',action='store_true',required=True)
    parser.add_argument('--runtime-role',required=True)
    args=parser.parse_args()
    try:
        with psycopg.connect(PostgresConfig(os.environ.get('DATABASE_URL','')).dsn,autocommit=True) as connection:migrate(connection,args.runtime_role)
        print('{"migration":"completed","notification_tables":5,"schema_version":"unchanged"}')
    except Exception:
        print('{"migration":"rejected","details":"Verify schema operator authority, source schema and restricted runtime role."}')
        raise SystemExit(1)
if __name__=='__main__':main()
