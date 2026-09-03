-- Zacon: let a user clear their own recorded statements.
--
-- Run in the Supabase SQL editor after 0003. The Insights "delete this period"
-- action removes a month/week of history; 0001 restricted DELETE to admins, so
-- this adds a self-service path scoped to rows the user recorded. Admins keep
-- the broader delete policy from 0001.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

create policy "users may delete their own statements"
  on public.statements for delete
  to authenticated
  using (created_by = auth.uid() or public.is_admin());
