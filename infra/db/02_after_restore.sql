-- Run as the server admin after the public schema and auth.users have been
-- restored from a Supabase backup. Safe to run again.

\set ON_ERROR_STOP on

-- The backup is taken with --no-privileges, so put back Supabase's grants.
-- Row level security, not these grants, decides what each user may touch.
grant usage on schema public to anon, authenticated, service_role;
grant all on all tables in schema public to anon, authenticated, service_role;
grant all on all sequences in schema public to anon, authenticated, service_role;
grant all on all functions in schema public to anon, authenticated, service_role;

-- A new login gets a profile (0001_init.sql). The trigger sits on auth.users,
-- which belongs to the login service, so it is created as that role.
grant usage on schema public to supabase_auth_admin;
grant execute on function public.handle_new_user() to supabase_auth_admin;
set role supabase_auth_admin;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();
reset role;

-- PostgREST reloads its schema cache when told to.
notify pgrst, 'reload schema';
