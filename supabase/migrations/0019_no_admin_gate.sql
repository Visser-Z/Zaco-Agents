-- Zacon: stop requiring admin to correct or remove records.
--
-- Run in the Supabase SQL editor. Deleting a period kept failing with
-- "your account doesn't have permission": every account starts as staff, and
-- UPDATE and DELETE on the operational tables were admin-only. PostgREST
-- answers a refused delete with 200 and an empty list, so for a long time it
-- read as success and the period stayed on screen.
--
-- This is a single-team internal tool. The whole history is already readable
-- and insertable by anyone who can sign in, so gating correction and removal
-- behind a second role protected nothing that reading it did not already
-- expose, while making a routine action impossible. The app still scopes every
-- delete to a chosen period behind a confirmation.
--
-- Written as a DO block rather than as named drops because these policies have
-- been created and recreated across several migrations, under several names.
-- Dropping every UPDATE and DELETE policy on these tables and putting back one
-- of each leaves the same result whatever state the database is in, and it can
-- be run again safely.
--
-- profiles is deliberately NOT included. It carries the role column, and its
-- admin policy is what stops an account granting itself rights. It blocks
-- nothing in day-to-day use.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

do $$
declare
  pol record;
begin
  for pol in
    select policyname, tablename
    from pg_policies
    where schemaname = 'public'
      and tablename in ('statements', 'payments', 'product_codes')
      and cmd in ('UPDATE', 'DELETE')
  loop
    execute format('drop policy if exists %I on public.%I', pol.policyname, pol.tablename);
  end loop;
end $$;

create policy statements_update
  on public.statements for update
  to authenticated
  using (auth.uid() is not null) with check (auth.uid() is not null);

create policy statements_delete
  on public.statements for delete
  to authenticated
  using (auth.uid() is not null);

create policy payments_update
  on public.payments for update
  to authenticated
  using (auth.uid() is not null) with check (auth.uid() is not null);

create policy payments_delete
  on public.payments for delete
  to authenticated
  using (auth.uid() is not null);

create policy product_codes_update
  on public.product_codes for update
  to authenticated
  using (auth.uid() is not null) with check (auth.uid() is not null);

create policy product_codes_delete
  on public.product_codes for delete
  to authenticated
  using (auth.uid() is not null);
