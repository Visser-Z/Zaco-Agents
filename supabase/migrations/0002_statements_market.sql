-- Zacon: capture the market/depot a consignment was sold at.
--
-- Run this in the Supabase SQL editor after 0001_init.sql. Adds the `market`
-- column the Daily Sales report carries in its "MARKET  Agent (Pre)" header.
-- It was previously parsed and discarded; keeping it is what makes per-market
-- analytics (best sellers by market, where a product moves fastest) possible.
--
-- Nullable on purpose: the older account-sales format has no market line, and
-- rows recorded before this migration have none either. Analytics buckets a
-- null market as "Unknown" rather than dropping the row.

alter table public.statements
  add column if not exists market text;

comment on column public.statements.market is
  'Market/depot from the Daily Sales header, e.g. "TSHWANE MARKET". Null on the account-sales format.';

create index if not exists statements_market_idx on public.statements (market);
