"""Explicit v4 operator migration; requires reviewed v3, never runs at app startup."""
MIGRATION_V4=r"""
BEGIN;
LOCK TABLE companion_private.schema_version IN EXCLUSIVE MODE;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM companion_private.schema_version WHERE version IN (3,4)) THEN
  RAISE EXCEPTION 'Workflow migration requires private schema 3 or 4';
 END IF;
END $$;
ALTER TABLE companion_private.schema_version DROP CONSTRAINT IF EXISTS schema_version_version_check;
ALTER TABLE companion_private.schema_version ADD CONSTRAINT schema_version_version_check CHECK(version IN (1,2,3,4));
UPDATE companion_private.schema_version SET version=4 WHERE version=3;
CREATE TABLE IF NOT EXISTS companion_private.pattern_jobs(
 account_id uuid NOT NULL REFERENCES companion_private.accounts(account_id),
 epoch text NOT NULL, job_id uuid NOT NULL, request_id uuid NOT NULL, utc_date date NOT NULL,
 origin text NOT NULL CHECK(origin IN ('nightly','self','operator')), actor_role text NOT NULL DEFAULT current_user,
 digest text NOT NULL CHECK(length(digest)=64),
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','complete','unavailable','cancelled','failed')),
 bank_status text NOT NULL DEFAULT 'not_checked' CHECK(bank_status IN ('not_checked','saved','refreshed','partial','unavailable')),
 reason text NOT NULL DEFAULT 'queued', attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
 available_at timestamptz NOT NULL, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
 lease_token uuid, lease_until timestamptz, run_id uuid,
 CHECK(status<>'running' OR (lease_token IS NOT NULL AND lease_until IS NOT NULL)),
 PRIMARY KEY(account_id,epoch,job_id), UNIQUE(account_id,epoch,request_id));
CREATE UNIQUE INDEX IF NOT EXISTS pattern_job_nightly_date ON companion_private.pattern_jobs(account_id,epoch,utc_date) WHERE origin='nightly';
CREATE UNIQUE INDEX IF NOT EXISTS pattern_job_running_account ON companion_private.pattern_jobs(account_id) WHERE status='running';
CREATE INDEX IF NOT EXISTS pattern_job_account_due ON companion_private.pattern_jobs(account_id,status,available_at);
ALTER TABLE companion_private.pattern_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE companion_private.pattern_jobs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS account_scope ON companion_private.pattern_jobs;
CREATE POLICY account_scope ON companion_private.pattern_jobs
 USING(account_id::text=current_setting('companion.account_id',true))
 WITH CHECK(account_id::text=current_setting('companion.account_id',true));
REVOKE ALL ON companion_private.pattern_jobs FROM PUBLIC;
DO $$ DECLARE r text; BEGIN
 FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
   EXECUTE format('REVOKE ALL ON companion_private.pattern_jobs FROM %I',r);
  END IF;
 END LOOP;
END $$;
COMMIT;
"""

def main():
    import argparse,os,psycopg
    from psycopg import sql
    from .postgres import PostgresConfig
    p=argparse.ArgumentParser(description='Reviewed UTC workflow migration and scoped runtime grant')
    p.add_argument('--confirm-utc-workflow-schema',action='store_true',required=True)
    p.add_argument('--runtime-role',required=True);a=p.parse_args()
    try:
        if a.runtime_role in {'postgres','PUBLIC','anon','authenticated'}:raise ValueError()
        with psycopg.connect(PostgresConfig(os.environ.get('DATABASE_URL','')).dsn,autocommit=True) as c:
            role=c.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s',(a.runtime_role,)).fetchone()
            if role is None or any(role):raise ValueError()
            with c.transaction():
                c.execute(MIGRATION_V4.strip().removeprefix('BEGIN;').removesuffix('COMMIT;'))
                c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON companion_private.pattern_jobs TO {}').format(sql.Identifier(a.runtime_role)))
        print('{"migration":"completed","version":4}')
    except Exception:
        print('{"migration":"rejected","details":"Verify schema and restricted operator authority."}')
        raise SystemExit(1)
if __name__=='__main__':main()
