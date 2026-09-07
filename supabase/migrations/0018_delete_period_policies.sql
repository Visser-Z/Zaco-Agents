-- Zacon: let a signed-in user delete a period completely.
--
-- Run in the Supabase SQL editor after 0017. Deleting a month removed the
-- sales but left the payments behind, so Tracking went on reporting the
-- period: the money paid and the money still owed both stood there against
-- sales that no longer existed. The delete was not failing loudly -- PostgREST
-- answers a delete that row-level security refuses with 200 and an empty list,
-- so it read as success.
--
-- The cause was the payments delete policy being admin-only while the app
-- offers the action to anyone who can reach Insights. This is the same fix
-- 0005 made for statements, for the same reason: this is a single-team
-- internal tool where the whole table is already readable and insertable by
-- any signed-in user, so an authenticated delete is consistent. The app still
-- scopes every delete to a chosen month or week behind a confirmation, and
-- there is no "delete everything" path.
--
-- The statements policy is asserted here as well. It was added in 0005, but a
-- database that skipped that migration shows exactly this symptom for the
-- sales instead of the payments, and re-asserting it costs nothing.
--
-- Safe to run more than once.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

drop policy if exists payments_delete on public.payments;

create policy payments_delete
  on public.payments for delete
  to authenticated
  using (auth.uid() is not null);

comment on table public.payments is
  'One row per account sale the market agent has paid. Deletable by any signed-in user so that deleting a period removes the whole period; scoped and confirmed in the app.';

drop policy if exists "authenticated users may delete statements" on public.statements;

create policy "authenticated users may delete statements"
  on public.statements for delete
  to authenticated
  using (auth.uid() is not null);
