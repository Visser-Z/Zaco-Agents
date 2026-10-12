-- Zacon: the market's own Stock On Hand, pulled from TechnoFresh every morning.
--
-- Run in the Supabase SQL editor after 0028. TechnoFresh report 28 lists every
-- delivery line with what was sent, what the market booked, what sold and what
-- is still on the floor, including lines nothing has sold from yet, which no
-- other report shows. It is a position at the moment it is run, so the 8am job
-- keeps one snapshot a day: every line of every market, under that day.
--
-- seq is the line's place within its FMS id as printed, which is its
-- consignment line number; the report prints no line number of its own.

create table if not exists public.market_stock (
  snapshot      date not null,
  market        text not null,
  fms_id        text not null,
  seq           integer not null,
  agent         text,
  supplier_ref  text,
  dn            bigint,
  delivered_on  date,
  product       text not null,
  sent          integer not null,
  amended       integer,
  sold          integer not null default 0,
  on_hand       integer not null default 0,
  avg_price     numeric,
  pulled_at     timestamptz not null default now(),
  primary key (snapshot, market, fms_id, seq)
);

create index if not exists market_stock_dn on public.market_stock (dn);

comment on table public.market_stock is
  'TechnoFresh Stock On Hand, one snapshot per day: every delivery line with sent, booked, sold and on hand.';

alter table public.market_stock enable row level security;

create policy market_stock_read on public.market_stock for select
  to authenticated using (true);
create policy market_stock_insert on public.market_stock for insert
  to authenticated with check (auth.uid() is not null);
create policy market_stock_delete on public.market_stock for delete
  to authenticated using (auth.uid() is not null);

-- The pull records each morning's snapshot beside the daily reports.
alter table public.technofresh_days drop constraint if exists technofresh_days_report_check;
alter table public.technofresh_days add constraint technofresh_days_report_check
  check (report in ('sales', 'payments', 'stock'));
