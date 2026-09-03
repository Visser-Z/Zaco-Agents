-- Zacon: Zaco's own delivery note number, which no export carries.
--
-- Run in the Supabase SQL editor after 0010. Column A of the workbook is Zaco's
-- DN. The market's exports carry a Delivery ID (theirs) and a Supplier Ref,
-- and the Supplier Ref only SOMETIMES holds the DN: measured against the
-- operator's own book over June, 26 of 43 statements agreed and 17 did not,
-- because the field held a producer code (20026*14013), a placeholder
-- (20026*20026), or another number entirely. In those cases the real DN appears
-- nowhere in the export at all, and one DN can cover several market deliveries.
--
-- So it is captured, the same way product short codes are: the operator corrects
-- it once for a delivery, and every row of that delivery gets it from then on.
--
-- Keyed on the market's Delivery ID because that is the one identifier always
-- present and never ambiguous.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

create table if not exists public.delivery_notes (
  delivery_id bigint primary key,
  dn          bigint not null,
  note        text,
  created_by  uuid references public.profiles(id) on delete set null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

comment on table public.delivery_notes is
  'Market Delivery ID -> Zaco''s own DN. Not derivable from any export; captured from the operator once per delivery.';

alter table public.delivery_notes enable row level security;

create policy "delivery notes are readable by authenticated users"
  on public.delivery_notes for select to authenticated using (true);
create policy "authenticated users may record delivery notes"
  on public.delivery_notes for insert to authenticated with check (auth.uid() is not null);
create policy "authenticated users may correct delivery notes"
  on public.delivery_notes for update to authenticated
  using (auth.uid() is not null) with check (auth.uid() is not null);

alter table public.statements
  add column if not exists delivery_id bigint;
