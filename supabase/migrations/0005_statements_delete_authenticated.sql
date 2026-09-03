-- Zacon: let any signed-in user delete recorded statements.
--
-- The Insights "delete this period" action was returning 200 but removing
-- nothing: the only DELETE policies were admin-only (0001) and own-rows (0004),
-- so a staff user, or rows whose created_by doesn't match, are silently
-- filtered out by RLS and no rows are deleted.
--
-- For this single-team internal tool the whole table is already readable and
-- insertable by any authenticated user, so allowing authenticated DELETE is
-- consistent and makes the period-delete work regardless of who recorded the
-- rows. The app still scopes every delete to a chosen month/week with a
-- confirmation, so there is no "delete everything" path.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

create policy "authenticated users may delete statements"
  on public.statements for delete
  to authenticated
  using (auth.uid() is not null);
