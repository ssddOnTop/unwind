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
FUNCTIONS = ('report_notification_terminal', 'report_notification_invalidate', 'report_notification_correction')

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


CURRENT_ASSESSMENT_DDL = r"""
DO $$ BEGIN
 IF to_regclass('companion_private.pattern_revisions') IS NULL OR NOT EXISTS(
  SELECT 1 FROM information_schema.columns WHERE table_schema='companion_private' AND table_name='pattern_runs' AND column_name='assessment') THEN
  RAISE EXCEPTION 'Apply the reviewed current-assessment migration first';
 END IF;
END $$;
LOCK TABLE companion_private.report_notification_outbox IN EXCLUSIVE MODE;
SELECT set_config('companion.notification_upgrade',CASE WHEN EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='companion_private' AND table_name='report_notification_outbox' AND column_name='actionability_key') THEN '0' ELSE '1' END,true);
ALTER TABLE companion_private.report_notification_outbox ADD COLUMN IF NOT EXISTS actionability_key text CHECK(actionability_key ~ '^[a-f0-9]{64}$');
DO $$ DECLARE c record; BEGIN
 FOR c IN SELECT conname FROM pg_constraint WHERE conrelid='companion_private.report_notification_outbox'::regclass
  AND pg_get_constraintdef(oid)='UNIQUE (account_id, epoch, run_id, installation_id)' LOOP
  EXECUTE format('ALTER TABLE companion_private.report_notification_outbox DROP CONSTRAINT %I',c.conname);
 END LOOP;
END $$;
-- Preserve existing accepted/uncertain delivery suppression at upgrade. The old
-- consumer's authority was exactly these frozen insight rows, not revisions.
SET LOCAL row_security=off;
DROP INDEX IF EXISTS companion_private.report_notification_actionability;
UPDATE companion_private.report_notification_outbox o SET actionability_key=k.key FROM (
 SELECT account_id,run_id,encode(sha256(convert_to(json_agg(concern_id ORDER BY concern_id)::text,'UTF8')),'hex') AS key
 FROM companion_private.pattern_insights WHERE attribution<>'not_for_me' GROUP BY account_id,run_id
) k WHERE o.account_id=k.account_id AND o.run_id=k.run_id AND o.actionability_key IS NULL;
WITH ranked AS (
 SELECT account_id,notification_id,row_number() OVER(PARTITION BY account_id,epoch,installation_id,actionability_key
 ORDER BY (status='accepted') DESC,attempts DESC,notification_id) AS n
 FROM companion_private.report_notification_outbox WHERE actionability_key IS NOT NULL
) UPDATE companion_private.report_notification_outbox o SET actionability_key=NULL FROM ranked r
 WHERE o.account_id=r.account_id AND o.notification_id=r.notification_id AND r.n>1;
UPDATE companion_private.report_notification_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL WHERE status IN ('pending','sending') AND (actionability_key IS NULL OR current_setting('companion.notification_upgrade')='1');
CREATE UNIQUE INDEX report_notification_actionability ON companion_private.report_notification_outbox(account_id,epoch,installation_id,actionability_key) WHERE actionability_key IS NOT NULL;
SET LOCAL row_security=on;
CREATE OR REPLACE FUNCTION companion_private.report_notification_terminal()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $$
DECLARE current_epoch text; current_run uuid;
BEGIN
 SELECT body->'state'->>'data_epoch' INTO current_epoch FROM companion_private.snapshots WHERE account_id=NEW.account_id FOR UPDATE;
 SELECT run_id INTO current_run FROM companion_private.pattern_runs WHERE account_id=NEW.account_id ORDER BY analyzed_at DESC,sequence DESC LIMIT 1;
 IF current_run IS DISTINCT FROM NEW.run_id THEN RETURN NEW; END IF;
 UPDATE companion_private.report_notification_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL
  WHERE account_id=NEW.account_id AND status IN ('pending','sending');
 IF NEW.status<>'running' AND current_epoch IS NOT NULL THEN
  INSERT INTO companion_private.report_notification_events(account_id,epoch,run_id) VALUES(NEW.account_id,current_epoch,NEW.run_id) ON CONFLICT DO NOTHING;
 END IF;
 RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION companion_private.report_notification_correction()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $$
DECLARE current_epoch text; current_run uuid;
BEGIN
 IF NEW.kind<>'attribution' THEN RETURN NEW; END IF;
 SELECT body->'state'->>'data_epoch' INTO current_epoch FROM companion_private.snapshots WHERE account_id=NEW.account_id FOR UPDATE;
 SELECT run_id INTO current_run FROM companion_private.pattern_runs WHERE account_id=NEW.account_id ORDER BY analyzed_at DESC,sequence DESC LIMIT 1;
 IF current_run IS DISTINCT FROM NEW.run_id THEN RETURN NEW; END IF;
 UPDATE companion_private.report_notification_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL
  WHERE account_id=NEW.account_id AND status IN ('pending','sending');
 INSERT INTO companion_private.report_notification_events(account_id,epoch,run_id) VALUES(NEW.account_id,current_epoch,NEW.run_id)
  ON CONFLICT(account_id,epoch,run_id) DO UPDATE SET completed_at=clock_timestamp();
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS report_notification_correction ON companion_private.pattern_revisions;
CREATE TRIGGER report_notification_correction AFTER INSERT ON companion_private.pattern_revisions FOR EACH ROW EXECUTE FUNCTION companion_private.report_notification_correction();
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
        connection.execute(CURRENT_ASSESSMENT_DDL)
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
    parser.add_argument('--confirm-current-assessment-authority',action='store_true',required=True)
    parser.add_argument('--runtime-role',required=True)
    args=parser.parse_args()
    try:
        with psycopg.connect(PostgresConfig(os.environ.get('DATABASE_URL','')).dsn,autocommit=True) as connection:migrate(connection,args.runtime_role)
        print('{"migration":"completed","notification_tables":5,"schema_version":"unchanged","authority":"current_assessment"}')
    except Exception:
        print('{"migration":"rejected","details":"Verify schema operator authority, source schema and restricted runtime role."}')
        raise SystemExit(1)
if __name__=='__main__':main()
