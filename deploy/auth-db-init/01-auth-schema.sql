-- Prepare a plain Postgres for GoTrue.
--
-- GoTrue's first migration runs `CREATE TABLE IF NOT EXISTS auth.users` and
-- assumes the schema is already there. Supabase's own Postgres image ships it
-- pre-created; postgres:16-alpine does not, so GoTrue crash-loops on
-- `schema "auth" does not exist` with no hint that the schema is its own
-- prerequisite.
--
-- Runs once, at cluster initialisation, as POSTGRES_USER against POSTGRES_DB.
-- Because it is that user creating the schema, ownership lands on the same role
-- GoTrue connects as, so no explicit grant is needed. Only re-runs on an empty
-- data volume — changing this file does not affect an initialised database.

CREATE SCHEMA IF NOT EXISTS auth;

-- GoTrue keys its records on uuid. pgcrypto covers gen_random_uuid() on
-- Postgres builds where it is not already built in; uuid-ossp is what Supabase's
-- own image provides. Both are cheap and idempotent, and a missing uuid function
-- surfaces the same way the missing schema did — as a migration failure at boot.
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;
