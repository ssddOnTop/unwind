"""Additive Health delta storage. Importing this module never opens a connection or runs DDL.

An operator may call apply_migration or the explicit confirmation CLI. Importing
this module and application startup never execute migrations.
"""
from psycopg import sql

TABLES = ('health_incremental_streams', 'health_incremental_objects', 'health_incremental_days', 'health_incremental_acknowledgments')
MIGRATION_HEALTH_INCREMENTAL = r"""
DO $$ BEGIN
 IF to_regclass('companion_private.health_samples') IS NULL
    OR to_regclass('companion_private.health_import_pages') IS NULL THEN
  RAISE EXCEPTION 'Apply the existing pattern migration first';
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS companion_private.health_incremental_streams (
 account_id uuid NOT NULL REFERENCES companion_private.snapshots(account_id) ON DELETE CASCADE,
 epoch text NOT NULL, hk_type text NOT NULL CHECK(hk_type IN ('stepCount','sleepAnalysis','heartRate','restingHeartRate','workout')),
 stream_id uuid NOT NULL, next_sequence bigint NOT NULL CHECK(next_sequence>=0),
 bootstrapped boolean NOT NULL DEFAULT false,
 PRIMARY KEY(account_id,epoch,hk_type));
CREATE TABLE IF NOT EXISTS companion_private.health_incremental_objects (
 account_id uuid NOT NULL REFERENCES companion_private.snapshots(account_id) ON DELETE CASCADE,
 epoch text NOT NULL, hk_type text NOT NULL CHECK(hk_type IN ('stepCount','sleepAnalysis','heartRate','restingHeartRate','workout')),
 hk_uuid uuid NOT NULL, raw jsonb CHECK(raw IS NULL OR octet_length(raw::text)<=16384),
 start_at timestamptz, end_at timestamptz,
 excluded boolean NOT NULL DEFAULT false, deleted boolean NOT NULL DEFAULT false,
 batch_id uuid, sample_id text, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(account_id,epoch,hk_type,hk_uuid),
 CHECK(raw IS NOT NULL OR deleted OR excluded),
 CHECK((start_at IS NULL AND end_at IS NULL) OR end_at>=start_at));
CREATE INDEX IF NOT EXISTS health_incremental_object_days
 ON companion_private.health_incremental_objects(account_id,epoch,hk_type,start_at,end_at) WHERE NOT deleted AND NOT excluded;
CREATE TABLE IF NOT EXISTS companion_private.health_incremental_days (
 account_id uuid NOT NULL REFERENCES companion_private.snapshots(account_id) ON DELETE CASCADE,
 epoch text NOT NULL, day date NOT NULL,
 PRIMARY KEY(account_id,epoch,day));
-- Minimal replay proof: no Health payload/UUID list, and no age-based expiration.
-- Raw-history or generic-receipt retention must never erase this sequence obligation.
CREATE TABLE IF NOT EXISTS companion_private.health_incremental_acknowledgments (
 account_id uuid NOT NULL REFERENCES companion_private.snapshots(account_id) ON DELETE CASCADE,
 epoch text NOT NULL, request_id text NOT NULL,
 digest text NOT NULL CHECK(digest ~ '^[a-f0-9]{64}$'),
 response jsonb NOT NULL CHECK(octet_length(response::text)<=4096),
 PRIMARY KEY(account_id,epoch,request_id));
DO $$ DECLARE t text; r text; BEGIN
 FOREACH t IN ARRAY ARRAY['health_incremental_streams','health_incremental_objects','health_incremental_days','health_incremental_acknowledgments'] LOOP
  EXECUTE format('ALTER TABLE companion_private.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE companion_private.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('DROP POLICY IF EXISTS account_scope ON companion_private.%I',t);
  EXECUTE format('CREATE POLICY account_scope ON companion_private.%I USING(account_id::text=current_setting(''companion.account_id'',true)) WITH CHECK(account_id::text=current_setting(''companion.account_id'',true))',t);
  EXECUTE format('REVOKE ALL ON companion_private.%I FROM PUBLIC',t);
  FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
   IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
    EXECUTE format('REVOKE ALL ON companion_private.%I FROM %I',t,r);
   END IF;
  END LOOP;
 END LOOP;
END $$;
CREATE OR REPLACE FUNCTION companion_private.guard_health_snapshot_after_incremental() RETURNS trigger
 LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
 PERFORM generation FROM companion_private.accounts WHERE account_id=NEW.account_id FOR NO KEY UPDATE;
 IF EXISTS(SELECT 1 FROM companion_private.health_incremental_streams
           WHERE account_id=NEW.account_id AND epoch=NEW.body->>'data_epoch') THEN
  RAISE EXCEPTION 'Complete-window Health imports cannot overwrite an incremental stream'
    USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION companion_private.guard_health_snapshot_after_incremental() FROM PUBLIC;
DROP TRIGGER IF EXISTS guard_health_snapshot_after_incremental ON companion_private.health_import_pages;
CREATE TRIGGER guard_health_snapshot_after_incremental BEFORE INSERT OR UPDATE ON companion_private.health_import_pages
 FOR EACH ROW EXECUTE FUNCTION companion_private.guard_health_snapshot_after_incremental();
CREATE OR REPLACE FUNCTION companion_private.clear_health_incremental_epoch() RETURNS trigger
 LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
 IF OLD.body->'state'->>'data_epoch' IS DISTINCT FROM NEW.body->'state'->>'data_epoch' THEN
  DELETE FROM companion_private.health_incremental_objects WHERE account_id=NEW.account_id;
  DELETE FROM companion_private.health_incremental_days WHERE account_id=NEW.account_id;
  DELETE FROM companion_private.health_incremental_streams WHERE account_id=NEW.account_id;
  DELETE FROM companion_private.health_incremental_acknowledgments WHERE account_id=NEW.account_id;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION companion_private.clear_health_incremental_epoch() FROM PUBLIC;
DROP TRIGGER IF EXISTS clear_health_incremental_epoch ON companion_private.snapshots;
CREATE TRIGGER clear_health_incremental_epoch AFTER UPDATE OF body ON companion_private.snapshots
 FOR EACH ROW EXECUTE FUNCTION companion_private.clear_health_incremental_epoch();
"""


def apply_migration(connection, runtime_role):
    """Explicit operator-only call; all DDL and dedicated runtime grants are atomic.

    Does not change the shared schema_version negotiation. The new route must check
    deployment readiness before activation. Existing runtime must retain its current
    narrow grants on snapshots, receipts, accounts, health_samples and pattern_batches.
    """
    if runtime_role in {'anon', 'authenticated', 'postgres', 'PUBLIC'}:
        raise ValueError('Dedicated restricted runtime role required')
    with connection.transaction():
        if connection.execute('SELECT current_user').fetchone()[0]==runtime_role:
            raise ValueError('Schema operator must differ from runtime role')
        row = connection.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s', (runtime_role,)).fetchone()
        if row is None or any(row):
            raise ValueError('Dedicated restricted runtime role required')
        connection.execute(MIGRATION_HEALTH_INCREMENTAL)
        # Upgrade before ordinary generic-receipt cleanup. Only validated minimal DTOs
        # from the CURRENT epoch are copied; a pruned digest is never reconstructed.
        from .health_incremental import HealthIncrementalReceipt, ROUTE
        from psycopg.types.json import Jsonb
        previous_scope = connection.execute("SELECT current_setting('companion.account_id',true)").fetchone()[0]
        for (account,) in connection.execute('SELECT account_id FROM companion_private.accounts ORDER BY account_id').fetchall():
            account = str(account)
            connection.execute("SELECT set_config('companion.account_id',%s,true)",(account,))
            connection.execute('SELECT generation FROM companion_private.accounts WHERE account_id=%s FOR NO KEY UPDATE',(account,))
            receipts = connection.execute('''SELECT r.epoch,r.request_id,r.digest,r.response
                FROM companion_private.receipts r JOIN companion_private.snapshots s USING(account_id)
                WHERE r.account_id=%s AND r.route=%s AND r.epoch=s.body->'state'->>'data_epoch'
                AND r.response IS NOT NULL AND NOT EXISTS(
                    SELECT 1 FROM companion_private.health_incremental_acknowledgments a
                    WHERE a.account_id=r.account_id AND a.epoch=r.epoch AND a.request_id=r.request_id)''',(account,ROUTE)).fetchall()
            for epoch,request_id,digest,response in receipts:
                try:
                    receipt = HealthIncrementalReceipt.model_validate({**response,'expired':response.get('expired',0)})
                except (TypeError, ValueError, AttributeError):
                    raise ValueError('Invalid legacy Health acknowledgment') from None
                if receipt.request_id != request_id or receipt.data_epoch != epoch:
                    raise ValueError('Health receipt identity mismatch')
                connection.execute('INSERT INTO companion_private.health_incremental_acknowledgments(account_id,epoch,request_id,digest,response) VALUES(%s,%s,%s,%s,%s)',
                    (account,epoch,request_id,digest,Jsonb(receipt.model_dump(mode='json'))))
        connection.execute("SELECT set_config('companion.account_id',%s,true)",(previous_scope or '',))
        for table in TABLES:
            permissions = 'SELECT,INSERT,DELETE' if table == 'health_incremental_acknowledgments' else 'SELECT,INSERT,UPDATE,DELETE'
            if table == 'health_incremental_acknowledgments':
                connection.execute(sql.SQL('REVOKE UPDATE ON companion_private.{} FROM {}').format(sql.Identifier(table),sql.Identifier(runtime_role)))
            connection.execute(sql.SQL('GRANT '+permissions+' ON companion_private.{} TO {}').format(
                sql.Identifier(table), sql.Identifier(runtime_role)))


def main():
    import argparse,psycopg
    from .postgres import PostgresConfig
    parser=argparse.ArgumentParser(description='Explicit additive UUID Health ingestion schema; no data reset')
    parser.add_argument('--confirm-incremental-health-schema',action='store_true',required=True)
    parser.add_argument('--runtime-role',required=True)
    args=parser.parse_args()
    try:
        with psycopg.connect(PostgresConfig.from_env().dsn,autocommit=True) as c:apply_migration(c,args.runtime_role)
        print('{"migration":"completed","tables":4,"existing_health_data":"preserved"}')
    except Exception:
        print('{"migration":"rejected","details":"Verify schema operator authority and restricted runtime role."}')
        raise SystemExit(1)
if __name__=='__main__':main()
