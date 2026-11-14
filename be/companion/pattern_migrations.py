"""Explicit, additive operator migration; never applied by the runtime."""
MIGRATION_V3 = r"""
BEGIN;
LOCK TABLE companion_private.schema_version IN EXCLUSIVE MODE;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM companion_private.schema_version WHERE version IN (1,2,3)) THEN
  RAISE EXCEPTION 'Expected private schema version 1, 2 or 3';
 END IF;
END $$;
ALTER TABLE companion_private.schema_version DROP CONSTRAINT IF EXISTS schema_version_version_check;
ALTER TABLE companion_private.schema_version ADD CONSTRAINT schema_version_version_check CHECK(version IN (1,2,3));
UPDATE companion_private.schema_version SET version=3 WHERE version IN (1,2);
CREATE TABLE IF NOT EXISTS companion_private.pattern_batches (
 account_id uuid NOT NULL REFERENCES companion_private.accounts(account_id),
 batch_id uuid NOT NULL, dataset text NOT NULL CHECK(dataset ~ '^[a-z0-9-]{1,80}$'),
 provenance text NOT NULL CHECK(provenance IN ('synthetic','providerSandbox','realHealthKit')),
 start_at timestamptz NOT NULL, end_at timestamptz NOT NULL CHECK(end_at>=start_at),
 ingested_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(account_id,batch_id), UNIQUE(account_id,dataset));
CREATE TABLE IF NOT EXISTS companion_private.health_samples (
 account_id uuid NOT NULL, batch_id uuid NOT NULL, sample_id text NOT NULL,
 hk_type text NOT NULL CHECK(hk_type IN ('stepCount','sleepAnalysis','heartRate','restingHeartRate','workout')),
 kind text NOT NULL CHECK(kind IN ('dailyCumulative','category','quantity','workout')),
 start_at timestamptz NOT NULL, end_at timestamptz NOT NULL CHECK(end_at>=start_at),
 timezone text NOT NULL, value double precision CHECK(value>=0 AND value<'Infinity'::float8),
 unit text CHECK(unit IN ('count','bpm')), category text CHECK(category IN ('inBed','asleepCore','asleepDeep','asleepREM','awake','asleepUnspecified')),
 workout_activity text CHECK(workout_activity IN ('walking','running','cycling')),
 duration_seconds double precision CHECK(duration_seconds>=0), energy_kcal double precision CHECK(energy_kcal>=0),
 distance_m double precision CHECK(distance_m>=0), source_name text NOT NULL CHECK(length(source_name)<=80),
 revision integer NOT NULL DEFAULT 1 CHECK(revision>0), ingested_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(account_id,batch_id,sample_id),
 FOREIGN KEY(account_id,batch_id) REFERENCES companion_private.pattern_batches(account_id,batch_id) ON DELETE CASCADE,
 CHECK((hk_type='stepCount' AND kind='dailyCumulative' AND unit='count' AND value IS NOT NULL AND end_at>start_at)
 OR (hk_type IN ('heartRate','restingHeartRate') AND kind='quantity' AND unit='bpm' AND value IS NOT NULL)
 OR (hk_type='sleepAnalysis' AND kind='category' AND category IS NOT NULL AND end_at>start_at)
 OR (hk_type='workout' AND kind='workout' AND workout_activity IS NOT NULL AND duration_seconds IS NOT NULL AND end_at>start_at)));
CREATE TABLE IF NOT EXISTS companion_private.pattern_transactions (
 account_id uuid NOT NULL, batch_id uuid NOT NULL, transaction_id text NOT NULL,
 event_at timestamptz NOT NULL, timezone text NOT NULL, amount numeric(14,2) NOT NULL,
 currency text NOT NULL CHECK(currency ~ '^[A-Z]{3}$'),
 category text NOT NULL CHECK(category IN ('food_service','groceries','other')),
 ingested_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(account_id,batch_id,transaction_id),
 FOREIGN KEY(account_id,batch_id) REFERENCES companion_private.pattern_batches(account_id,batch_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS companion_private.pattern_runs (
 account_id uuid NOT NULL REFERENCES companion_private.accounts(account_id), run_id uuid NOT NULL,
 request_id uuid NOT NULL, input_digest text NOT NULL CHECK(length(input_digest)=64),
 input_version text NOT NULL, status text NOT NULL CHECK(status IN ('running','complete','unavailable')),
 engine text NOT NULL CHECK(engine IN ('deterministic','LLMassisted')),
 analyzed_at timestamptz NOT NULL DEFAULT now(), evidence jsonb NOT NULL,
 is_demo boolean NOT NULL, PRIMARY KEY(account_id,run_id), UNIQUE(account_id,request_id),
 UNIQUE(account_id,input_digest,input_version));
CREATE TABLE IF NOT EXISTS companion_private.pattern_requests (
 account_id uuid NOT NULL, request_id uuid NOT NULL, run_id uuid NOT NULL,
 PRIMARY KEY(account_id,request_id),
 FOREIGN KEY(account_id,run_id) REFERENCES companion_private.pattern_runs(account_id,run_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS companion_private.pattern_insights (
 account_id uuid NOT NULL, run_id uuid NOT NULL, concern_id text NOT NULL CHECK(concern_id IN ('activity','sleep','food_service')),
 attribution text NOT NULL DEFAULT 'unconfirmed' CHECK(attribution IN ('unconfirmed','for_me','not_for_me','unsure')),
 rank integer NOT NULL CHECK(rank BETWEEN 0 AND 2), PRIMARY KEY(account_id,run_id,concern_id),
 FOREIGN KEY(account_id,run_id) REFERENCES companion_private.pattern_runs(account_id,run_id) ON DELETE CASCADE);
ALTER TABLE companion_private.health_samples DROP CONSTRAINT IF EXISTS health_samples_category_check;
ALTER TABLE companion_private.health_samples ADD CONSTRAINT health_samples_category_check CHECK(category IN ('inBed','asleepCore','asleepDeep','asleepREM','awake','asleepUnspecified'));
ALTER TABLE companion_private.pattern_batches ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'operator' CHECK(origin IN ('operator','healthkit','plaid'));
ALTER TABLE companion_private.pattern_batches ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
ALTER TABLE companion_private.pattern_batches ADD COLUMN IF NOT EXISTS connection_id text;
ALTER TABLE companion_private.pattern_batches ADD COLUMN IF NOT EXISTS bank_account_id text;
ALTER TABLE companion_private.health_samples ADD COLUMN IF NOT EXISTS source_bundle text NOT NULL DEFAULT '';
ALTER TABLE companion_private.health_samples ADD COLUMN IF NOT EXISTS source_revision text NOT NULL DEFAULT '';
ALTER TABLE companion_private.health_samples ADD COLUMN IF NOT EXISTS deleted boolean NOT NULL DEFAULT false;
ALTER TABLE companion_private.pattern_transactions ADD COLUMN IF NOT EXISTS deleted boolean NOT NULL DEFAULT false;
ALTER TABLE companion_private.pattern_runs ADD COLUMN IF NOT EXISTS source_label text NOT NULL DEFAULT 'Previously saved review';
ALTER TABLE companion_private.pattern_runs ADD COLUMN IF NOT EXISTS brief_override text CHECK(length(brief_override)<=4000);
CREATE TABLE IF NOT EXISTS companion_private.health_import_pages (
 account_id uuid NOT NULL REFERENCES companion_private.accounts(account_id), collection_id uuid NOT NULL,
 page_index integer NOT NULL CHECK(page_index BETWEEN 0 AND 499), digest text NOT NULL,
 body jsonb NOT NULL CHECK(octet_length(body::text)<=262144), received_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(account_id,collection_id,page_index));
CREATE INDEX IF NOT EXISTS health_sample_account_date_type ON companion_private.health_samples(account_id,start_at,hk_type);
CREATE INDEX IF NOT EXISTS pattern_transaction_account_date ON companion_private.pattern_transactions(account_id,event_at,category);
CREATE INDEX IF NOT EXISTS pattern_run_account_date ON companion_private.pattern_runs(account_id,analyzed_at DESC);
DO $$ DECLARE t text; r text; BEGIN
 FOREACH t IN ARRAY ARRAY['pattern_batches','health_samples','pattern_transactions','pattern_runs','pattern_insights','pattern_requests','health_import_pages'] LOOP
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
COMMIT;
"""


from .assessment_migrations import ASSESSMENT_MIGRATION
MIGRATION_V3 = MIGRATION_V3.replace('COMMIT;', ASSESSMENT_MIGRATION + '\nCOMMIT;')

def main():
    import argparse
    import os
    import psycopg
    from psycopg import sql
    from .postgres import PostgresConfig
    p=argparse.ArgumentParser(description='Explicit private pattern migration and narrowly scoped runtime grants')
    p.add_argument('--confirm-private-pattern-schema',action='store_true',required=True)
    p.add_argument('--runtime-role',required=True)
    a=p.parse_args()
    if a.runtime_role in {'anon','authenticated','postgres','PUBLIC'}:p.error('A dedicated restricted runtime role is required')
    try:
        config=PostgresConfig(os.environ.get('DATABASE_URL',''))
        with psycopg.connect(config.dsn,autocommit=True) as c:
            row=c.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s',(a.runtime_role,)).fetchone()
            if row is None or any(row):raise ValueError('Restricted role required')
            # Keep reviewed DDL and role grants atomic; the exported script also works standalone.
            ddl=MIGRATION_V3.strip().removeprefix('BEGIN;').removesuffix('COMMIT;')
            with c.transaction():
                c.execute(ddl)
                for table in ('pattern_batches','health_samples','pattern_transactions','pattern_runs','pattern_insights','pattern_requests','health_import_pages','pattern_revisions'):
                    c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON companion_private.{} TO {}').format(sql.Identifier(table),sql.Identifier(a.runtime_role)))
        print('{"migration":"completed","runtime_grants":"scoped"}')
    except Exception:
        print('{"migration":"rejected","details":"Verify operator DDL authority, schema version and restricted role."}')
        raise SystemExit(1)

if __name__=='__main__':main()
