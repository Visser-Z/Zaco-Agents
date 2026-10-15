-- The Supabase pieces the app relies on, recreated on Azure PostgreSQL.
-- Run as the server admin, with the two passwords passed as psql variables:
--   psql ... -v authenticator_pw=... -v auth_admin_pw=... -f 01_supabase_roles.sql
-- Safe to run again.

\set ON_ERROR_STOP on

-- The roles PostgREST switches into, from the "role" claim of the caller's JWT.
do $$
begin
  if not exists (select from pg_roles where rolname = 'anon') then
    create role anon nologin noinherit;
  end if;
  if not exists (select from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit;
  end if;
  if not exists (select from pg_roles where rolname = 'service_role') then
    create role service_role nologin noinherit;
  end if;
  if not exists (select from pg_roles where rolname = 'authenticator') then
    create role authenticator login noinherit;
  end if;
  -- Supabase always has a "postgres" role and the login service's migrations
  -- grant to it. On Azure it exists only to receive those grants.
  if not exists (select from pg_roles where rolname = 'postgres') then
    create role postgres nologin;
  end if;
  if not exists (select from pg_roles where rolname = 'supabase_auth_admin') then
    create role supabase_auth_admin login noinherit;
  end if;
end $$;

alter role authenticator password :'authenticator_pw';
alter role supabase_auth_admin password :'auth_admin_pw';

grant anon, authenticated, service_role to authenticator;
-- The admin restores and owns the public tables and must be able to hand
-- objects to these roles.
grant anon, authenticated, service_role, supabase_auth_admin to current_user;

-- The login service owns its own schema and migrates it on start.
create schema if not exists auth authorization supabase_auth_admin;
alter role supabase_auth_admin set search_path = auth;
grant usage on schema auth to anon, authenticated, service_role;
grant create on database postgres to supabase_auth_admin;

create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;
create extension if not exists "uuid-ossp" with schema extensions;
grant usage on schema extensions to anon, authenticated, service_role, supabase_auth_admin;

-- Supabase's defaults for the app's own schema. Row level security, not these
-- grants, decides what each signed-in user may touch.
grant usage on schema public to anon, authenticated, service_role;
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;

alter role authenticator set statement_timeout = '30s';
alter role anon set statement_timeout = '3s';
alter role authenticated set statement_timeout = '30s';
