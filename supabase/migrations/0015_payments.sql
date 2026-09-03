-- Zacon: remember payments, so outstanding money can be tracked over time.
--
-- Run in the Supabase SQL editor after 0014. Until now reconciling a Payment
-- Details report was read-only: it showed paid-vs-outstanding on screen and
-- forgot it on refresh, so nothing could report what was still owed across the
-- whole period. This table is the durable record of every payment the agent
-- has reported, one row per account sale.
--
-- The commodity lines are kept as JSON on the row. The daily-sales PDF prints
-- no payment reference, so a sale is matched to its payment on supplier ref
-- plus product, which needs each payment's per-product breakdown. Storing the
-- lines with the payment keeps that match possible from saved data alone,
-- rather than only in the moment a file is dropped.
--
-- Idempotent: the AccSale number is the key. Re-processing the same payment
-- report is ignored rather than duplicated. Like a statement, a recorded
-- payment is a financial record staff do not overwrite; an admin may correct
-- one through the update path.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

create table if not exists public.payments (
  accsale        text primary key,
  stm_no         bigint,
  market_agent   text,
  supplier_ref   text,
  dn             bigint,
  paid_on        date,
  nett           numeric,
  gross          numeric,
  deductions     numeric,
  vat            numeric,
  lines          jsonb not null default '[]'::jsonb,
  created_by     uuid references public.profiles(id) on delete set null,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

comment on table public.payments is
  'One row per account sale the market agent has paid. The durable record behind the outstanding-payments tracker; sales rows are never touched.';

create index if not exists payments_dn_idx on public.payments (dn);
create index if not exists payments_paid_on_idx on public.payments (paid_on);

alter table public.payments enable row level security;

-- Anyone signed in may read the payments, exactly as they may read statements:
-- the tracker is for the whole team.
create policy payments_read
  on public.payments for select
  to authenticated
  using (true);

-- Staff may record payments (dropping in a Payment Details report).
create policy payments_insert
  on public.payments for insert
  to authenticated
  with check (auth.uid() is not null);

-- A recorded payment is a financial record. Only an admin may change or remove
-- one; re-processing a report refreshes it through the insert path's upsert.
create policy payments_update
  on public.payments for update
  to authenticated
  using (public.is_admin())
  with check (public.is_admin());

create policy payments_delete
  on public.payments for delete
  to authenticated
  using (public.is_admin());
