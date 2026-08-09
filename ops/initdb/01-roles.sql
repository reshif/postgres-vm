-- =============================================================================
-- ops/initdb/01-roles.sql
-- Runs once, on first initialisation of the pgdata volume, as memory_owner.
--
-- Creates the three application roles and the PgBouncer auth_query plumbing.
-- If you change this file after the volume exists, it will NOT re-run — either
-- `docker compose down -v` (destroys data) or apply the delta by hand.
-- =============================================================================

\set app_password `echo "$DB_APP_PASSWORD"`
\set ro_password `echo "$DB_RO_PASSWORD"`
\set pgb_password `echo "$DB_PGBOUNCER_PASSWORD"`

-- SCRAM is the PG18 default; keep it explicit so the verifier format is known.
SET password_encryption = 'scram-sha-256';

-- ---------------------------------------------------------------- app roles
-- NOBYPASSRLS is the important word here. Without it, a role with sufficient
-- privilege can ignore every policy in 01-SCHEMA.sql and nothing errors —
-- the isolation tests would pass while production leaked.
CREATE ROLE memory_app  LOGIN NOBYPASSRLS PASSWORD :'app_password';
CREATE ROLE memory_ro   LOGIN NOBYPASSRLS PASSWORD :'ro_password';

-- memory_owner owns the schema (created by the image from POSTGRES_USER) and is
-- deliberately NOT the role the application connects as. FORCE ROW LEVEL SECURITY
-- covers the owner too, but keeping them separate means a mistake in one place is
-- not sufficient on its own.

-- ------------------------------------------------------- pgbouncer auth_query
-- PgBouncer authenticates clients by asking Postgres for the stored verifier,
-- rather than us maintaining a full userlist.txt with every application password.
-- Only pgbouncer_auth's own credential lives in userlist.txt.
CREATE ROLE pgbouncer_auth LOGIN NOBYPASSRLS PASSWORD :'pgb_password';

CREATE SCHEMA IF NOT EXISTS pgbouncer AUTHORIZATION pgbouncer_auth;

CREATE OR REPLACE FUNCTION pgbouncer.get_auth(p_usename text)
RETURNS TABLE (username text, password text)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  SELECT rolname::text, rolpassword::text
    FROM pg_authid
   WHERE rolname = p_usename
     AND rolcanlogin
     -- Never hand out the superuser/owner verifier through the pooler path.
     AND rolname <> 'memory_owner'
     AND NOT rolsuper;
$$;

REVOKE ALL ON FUNCTION pgbouncer.get_auth(text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION pgbouncer.get_auth(text) TO pgbouncer_auth;
GRANT  USAGE ON SCHEMA pgbouncer TO pgbouncer_auth;

-- ------------------------------------------------------------- db privileges
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT  CONNECT ON DATABASE memory TO memory_app, memory_ro, pgbouncer_auth;

-- Schema-level grants for mem.* are issued by the migration that creates it
-- (see the GRANT block at the end of 01-SCHEMA.sql). Nothing to do here.

-- ------------------------------------------------------------- sanity checks
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN ('memory_app','memory_ro')
              AND rolbypassrls) THEN
    RAISE EXCEPTION 'application roles must be NOBYPASSRLS';
  END IF;
END $$;
