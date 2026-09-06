-- Zacon: let the operator close a Tracking item they have dealt with.
--
-- Run in the Supabase SQL editor after 0015. The Tracking lists are computed
-- fresh from the history on every load, so anything still open reappears for
-- ever -- including the lines someone has already chased, written off or sold
-- off the floor. There was no way to say "this one is handled", so the lists
-- only grew and stopped being read.
--
-- One row per closed item. The item is named by what it is and which one it is
-- (kind + ref) rather than by a row id, because the lists are recomputed and
-- carry no stable id of their own: a slow-stock line is a consignment, an
-- outstanding line is a delivery note plus product.
--
-- Closing is a note, never a deletion: the sale and the payment are untouched,
-- and reopening is just removing the row. Anyone signed in may close and
-- reopen, because this is day-to-day operational marking rather than a
-- financial record.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

create table if not exists public.dismissals (
  kind         text not null,
  ref          text not null,
  note         text,
  created_by   uuid references public.profiles(id) on delete set null,
  created_at   timestamptz not null default now(),
  primary key (kind, ref)
);

comment on table public.dismissals is
  'Tracking items the team has closed off. Advisory only: nothing in the sales or payment history is changed by closing one.';

alter table public.dismissals enable row level security;

create policy dismissals_read
  on public.dismissals for select
  to authenticated
  using (true);

create policy dismissals_insert
  on public.dismissals for insert
  to authenticated
  with check (auth.uid() is not null);

-- Reopening is the ordinary counterpart of closing, so it is not admin-only.
create policy dismissals_delete
  on public.dismissals for delete
  to authenticated
  using (auth.uid() is not null);
