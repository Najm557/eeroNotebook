-- Prepare a plain Postgres for Supabase's GoTrue.
--
-- Two things GoTrue's migrations assume exist and never create:
--
--   1. The `auth` schema. Its first migration runs
--      `CREATE TABLE IF NOT EXISTS auth.users` and fails with
--      `schema "auth" does not exist`.
--   2. Supabase's role set. `20240612123726_enable_rls_update_grants` runs
--      `grant ... to postgres, dashboard_user` and fails with
--      `role "postgres" does not exist` on a cluster whose superuser is named
--      anything else.
--
-- Supabase's own Postgres image provides both. postgres:16-alpine provides
-- neither, so running their GoTrue here means supplying what its migrations
-- take for granted.
--
-- Runs once, at cluster initialisation, as POSTGRES_USER against POSTGRES_DB.
-- It does NOT re-run on an initialised volume, so a missing prerequisite costs a
-- crash-loop and a wiped data directory to correct — which is why the role list
-- below is generous rather than minimal. Every role is NOLOGIN: they exist to
-- satisfy grants, and nothing ever authenticates as them.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
    CREATE ROLE postgres NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_user') THEN
    CREATE ROLE dashboard_user NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    CREATE ROLE anon NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    CREATE ROLE authenticated NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    CREATE ROLE service_role NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'supabase_auth_admin') THEN
    CREATE ROLE supabase_auth_admin NOLOGIN;
  END IF;
END
$$;

-- Created by POSTGRES_USER, so ownership lands on the role GoTrue connects as
-- and no explicit grant is needed.
CREATE SCHEMA IF NOT EXISTS auth;

-- GoTrue keys its records on uuid. A missing uuid function would surface exactly
-- as the two problems above did: a migration failure at boot, with the cause
-- buried in a dump of the whole statement.
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;
