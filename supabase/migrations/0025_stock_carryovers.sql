-- Zacon: stock still on the floor at the end of a month, carried into the next.
--
-- Run in the Supabase SQL editor after 0024. Stock on Hand is filed under the
-- month it arrived, so on the first of a new month whatever is still unsold
-- from the last one drops out of the new month's view. At the start of each
-- month the app asks whether to carry it over; each row here is one
-- consignment carried into one month, with what was on the floor when it was.
--
-- Nothing about sales or payments moves: a carton sold in the new month is
-- that month's sale, and the money for it lands in that month, as it always
-- did. This only says where the unsold stock is shown.
--
-- Shared by the whole team, like closed Tracking lines.

create table if not exists public.stock_carryovers (
  month          text not null check (month ~ '^\d{4}-\d{2}$'),
  ref            text not null,
  consignment_id bigint,
  product        text,
  market         text,
  cartons        integer,
  arrived        date,
  created_by     uuid references public.profiles(id) on delete set null,
  created_at     timestamptz not null default now(),
  primary key (month, ref)
);

comment on table public.stock_carryovers is
  'Unsold stock carried from one month into the next, so the new month''s Stock on Hand shows it.';

alter table public.stock_carryovers enable row level security;

create policy stock_carryovers_read on public.stock_carryovers for select
  to authenticated using (true);
create policy stock_carryovers_insert on public.stock_carryovers for insert
  to authenticated with check (auth.uid() is not null);
create policy stock_carryovers_update on public.stock_carryovers for update
  to authenticated using (auth.uid() is not null) with check (auth.uid() is not null);
create policy stock_carryovers_delete on public.stock_carryovers for delete
  to authenticated using (auth.uid() is not null);
