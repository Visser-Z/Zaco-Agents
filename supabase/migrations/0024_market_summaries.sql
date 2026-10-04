-- Zacon: the market's own word on what it has paid, per delivery and product.
--
-- Run in the Supabase SQL editor after 0023. The FMS "Summary of Deliveries By
-- Agent" report prints, for every delivery sent in its date range, what the
-- market sold and what it says it has paid and not paid. Dropped in with the
-- payments, it is kept here, and every outstanding line on the Tracking tab is
-- checked against it: the market agrees it is unpaid, or says it is paid (and
-- the app names the payment dates to fetch), or no summary covers it yet.
--
-- One row per delivery and product (the product as the app normalises it, so
-- the report's spelling and the sales report's meet). A later run of the report
-- replaces an earlier one; an older run never overwrites a newer one, which the
-- app checks before writing.
--
-- Shared by the whole team, like the payments it is checked against.

create table if not exists public.market_summaries (
  delivery_id   bigint not null,
  product       text not null,
  product_name  text,
  agent         text,
  supplier_ref  text,
  date_sent     date,
  qty_sent      integer,
  sold          integer,
  gross         numeric(14, 2),
  paid          numeric(14, 2),
  unpaid        numeric(14, 2),
  run_at        timestamp,
  period_from   date,
  period_to     date,
  source_file   text,
  created_by    uuid references public.profiles(id) on delete set null,
  created_at    timestamptz not null default now(),
  primary key (delivery_id, product)
);

comment on table public.market_summaries is
  'The market''s Summary of Deliveries By Agent: sold, paid and unpaid per delivery and product, as of the report''s run.';

alter table public.market_summaries enable row level security;

create policy market_summaries_read
  on public.market_summaries for select
  to authenticated
  using (true);

create policy market_summaries_insert
  on public.market_summaries for insert
  to authenticated
  with check (auth.uid() is not null);

create policy market_summaries_update
  on public.market_summaries for update
  to authenticated
  using (auth.uid() is not null)
  with check (auth.uid() is not null);

create policy market_summaries_delete
  on public.market_summaries for delete
  to authenticated
  using (auth.uid() is not null);
