"""Additive assessment history migration, included by the explicit pattern operator.

No runtime DDL. Existing runs are backfilled once from their saved evidence, never
from today's raw records. Deletes remain available for authorized account erasure.
"""
ASSESSMENT_MIGRATION = r"""
ALTER TABLE companion_private.pattern_runs ADD COLUMN IF NOT EXISTS assessment jsonb;
ALTER TABLE companion_private.pattern_runs ADD COLUMN IF NOT EXISTS sequence bigint GENERATED ALWAYS AS IDENTITY;
-- Identical evidence may recur after an intervening assessment. Reuse its model
-- result, but append a new assessment rather than reactivating a historical row.
ALTER TABLE companion_private.pattern_runs DROP CONSTRAINT IF EXISTS pattern_runs_account_id_input_digest_input_version_key;
CREATE INDEX IF NOT EXISTS pattern_cache_lookup ON companion_private.pattern_runs(account_id,input_digest,input_version,analyzed_at DESC,sequence DESC);
CREATE INDEX IF NOT EXISTS pattern_current_lookup ON companion_private.pattern_runs(account_id,analyzed_at DESC,sequence DESC);
CREATE TABLE IF NOT EXISTS companion_private.pattern_revisions (
 account_id uuid NOT NULL, run_id uuid NOT NULL,
 revision bigint GENERATED ALWAYS AS IDENTITY,
 kind text NOT NULL CHECK(kind IN ('attribution','notes')),
 concern_id text CHECK(concern_id IN ('activity','sleep','food_service')),
 value text NOT NULL CHECK(length(value)<=4000),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(account_id,run_id,revision),
 FOREIGN KEY(account_id,run_id) REFERENCES companion_private.pattern_runs(account_id,run_id) ON DELETE CASCADE,
 CHECK((kind='notes' AND concern_id IS NULL) OR
       (kind='attribution' AND concern_id IS NOT NULL AND value IN ('for_me','not_for_me','unsure')))
);
ALTER TABLE companion_private.pattern_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE companion_private.pattern_revisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS account_scope ON companion_private.pattern_revisions;
CREATE POLICY account_scope ON companion_private.pattern_revisions
 USING(account_id::text=current_setting('companion.account_id',true))
 WITH CHECK(account_id::text=current_setting('companion.account_id',true));
REVOKE ALL ON companion_private.pattern_revisions FROM PUBLIC;
DO $$ DECLARE r text; BEGIN
 FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
   EXECUTE format('REVOKE ALL ON companion_private.pattern_revisions FROM %I',r);
  END IF;
 END LOOP;
END $$;
-- Backfill under operator authority before installing immutable-update guards.
-- Existing attributions and notes remain in their original immutable columns.
UPDATE companion_private.pattern_runs r SET assessment=(
 SELECT jsonb_build_object('areas',jsonb_agg(jsonb_build_object(
  'area',a.area,'state',CASE WHEN i.concern_id IS NOT NULL THEN 'concern'
   WHEN (r.evidence->'recent'->>a.missing)::boolean OR (r.evidence->'baseline'->>a.missing)::boolean THEN 'insufficient_data'
   ELSE 'no_current_concern' END,
  'summary',CASE WHEN i.concern_id IS NOT NULL THEN a.explanation
   WHEN (r.evidence->'recent'->>a.missing)::boolean OR (r.evidence->'baseline'->>a.missing)::boolean THEN 'Not enough recorded coverage to assess this area.'
   ELSE 'No current concern supported by this comparison. This does not establish good health.' END) ORDER BY a.rank),
  'findings',COALESCE(jsonb_agg(jsonb_build_object('concern_id',a.area,'title',a.title,
   'explanation',a.explanation,'evidence_codes',jsonb_build_array(a.code),
   'confidence','limited','attribution',i.attribution,'evidence_summary',a.explanation)
   ORDER BY i.rank) FILTER(WHERE i.concern_id IS NOT NULL),'[]'::jsonb))
 FROM (VALUES
  ('activity','missing_steps','Lower recorded activity','Recent recorded daily steps are lower than the baseline. Consider whether your routine or recording coverage changed.','steps_comparison',0),
  ('sleep','missing_sleep','Shorter recorded sleep','Recent recorded asleep intervals are shorter than the baseline. This is not a diagnosis and may reflect missing recordings.','sleep_comparison',1),
  ('food_service','missing_transactions','More food-service purchases','Food-service purchase frequency increased. Purchases do not establish who ate the food, nutrition or diet quality.','purchase_frequency',2)
 ) a(area,missing,title,explanation,code,rank)
 LEFT JOIN companion_private.pattern_insights i ON i.account_id=r.account_id AND i.run_id=r.run_id AND i.concern_id=a.area
) WHERE assessment IS NULL;
CREATE OR REPLACE FUNCTION companion_private.guard_assessment_update() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
 IF TG_TABLE_NAME='pattern_runs' THEN
  IF OLD.status<>'running' THEN RAISE EXCEPTION 'Completed assessment is immutable'; END IF;
 ELSIF TG_TABLE_NAME='pattern_revisions' THEN
  RAISE EXCEPTION 'Assessment revision is immutable';
 ELSE
  IF EXISTS(SELECT 1 FROM companion_private.pattern_runs WHERE account_id=OLD.account_id AND run_id=OLD.run_id AND status<>'running') THEN
   RAISE EXCEPTION 'Completed assessment insight is immutable';
  END IF;
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS immutable_assessment ON companion_private.pattern_runs;
CREATE TRIGGER immutable_assessment BEFORE UPDATE ON companion_private.pattern_runs
 FOR EACH ROW EXECUTE FUNCTION companion_private.guard_assessment_update();
DROP TRIGGER IF EXISTS immutable_insight ON companion_private.pattern_insights;
CREATE TRIGGER immutable_insight BEFORE UPDATE ON companion_private.pattern_insights
 FOR EACH ROW EXECUTE FUNCTION companion_private.guard_assessment_update();
DROP TRIGGER IF EXISTS immutable_revision ON companion_private.pattern_revisions;
CREATE TRIGGER immutable_revision BEFORE UPDATE ON companion_private.pattern_revisions
 FOR EACH ROW EXECUTE FUNCTION companion_private.guard_assessment_update();
"""


ASSESSMENT_TABLES = ('pattern_runs', 'pattern_insights', 'pattern_requests', 'pattern_revisions')


def validate_runtime_role(conn, role):
    if role in {'anon', 'authenticated', 'postgres', 'PUBLIC'}:
        raise ValueError('A dedicated restricted runtime role is required')
    flags = conn.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s', (role,)).fetchone()
    if flags is None or any(flags):
        raise ValueError('A dedicated restricted runtime role is required')


def upgrade_assessments(conn, runtime_role):
    """Explicit operator entry point; atomically upgrade v3/v4 without rewriting its marker."""
    from psycopg import sql
    with conn.transaction():
        conn.execute('LOCK TABLE companion_private.schema_version IN EXCLUSIVE MODE')
        versions = conn.execute('SELECT version FROM companion_private.schema_version').fetchall()
        if versions not in ([(3,)], [(4,)]):
            raise ValueError('Assessment upgrade requires private schema 3 or 4')
        validate_runtime_role(conn, runtime_role)
        # Fail rather than silently backfill only the operator's RLS-visible rows.
        # This does not grant BYPASSRLS to either the operator or runtime role.
        conn.execute('SET LOCAL row_security=off')
        conn.execute(ASSESSMENT_MIGRATION)
        for table in ASSESSMENT_TABLES:
            conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON companion_private.{} TO {}').format(sql.Identifier(table), sql.Identifier(runtime_role)))
        return versions[0][0]


def main():
    import argparse
    import json
    import os
    import psycopg
    from .postgres import PostgresConfig
    parser = argparse.ArgumentParser(description='Assessment-only upgrade for existing private schema v3 or v4; preserves schema version and data')
    parser.add_argument('--confirm-current-assessment-schema', action='store_true', required=True)
    parser.add_argument('--runtime-role', required=True)
    args = parser.parse_args()
    try:
        with psycopg.connect(PostgresConfig(os.environ.get('DATABASE_URL', '')).dsn, autocommit=True) as conn:
            version = upgrade_assessments(conn, args.runtime_role)
        print(json.dumps({'migration': 'completed', 'version': version, 'runtime_grants': 'assessment-scoped'}))
    except Exception:
        print('{"migration":"rejected","details":"Verify schema v3/v4 and restricted operator authority."}')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
