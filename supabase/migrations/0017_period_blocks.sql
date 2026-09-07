-- Zacon: address the history one month at a time, and report on any date range.
--
-- Run in the Supabase SQL editor after 0016. The workbook kept a sheet per
-- month and that is how the operator still thinks about the book: August is a
-- thing you open, close and hand over. The history had no such handle -- every
-- read scanned the whole table and filtered on a date range in the client.
--
-- This gives each row the month it belongs to as a stored, indexed value, so a
-- month is a block the database can go straight to. The column is GENERATED:
-- it is derived from the date already on the row, so it cannot drift out of
-- step with it and there is nothing to keep in sync on write.
--
-- period_index then lists those blocks the way the workbook's tabs did: one
-- line per month, what it holds, and what it is worth. It is a view, so it is
-- never stale, and it is security_invoker so the caller's RLS decides what
-- they can see -- a view that ran as its owner would hand every user the whole
-- table.
--
-- Nothing is moved and nothing is rewritten. Existing rows get their month
-- filled in by the generated column as part of this migration.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

alter table public.statements
  add column if not exists period_month text
  generated always as (to_char(group_date, 'YYYY-MM')) stored;

comment on column public.statements.period_month is
  'The month this sale belongs to (YYYY-MM), derived from group_date. The workbook''s sheet name, as an indexed column.';

create index if not exists statements_period_month_idx
  on public.statements (period_month);

-- Reports run over a date range far more often than over anything else.
create index if not exists statements_group_date_idx
  on public.statements (group_date);

alter table public.payments
  add column if not exists period_month text
  generated always as (to_char(paid_on, 'YYYY-MM')) stored;

comment on column public.payments.period_month is
  'The month this payment belongs to (YYYY-MM), derived from paid_on.';

create index if not exists payments_period_month_idx
  on public.payments (period_month);

-- One line per month of sales: the tab strip of the old workbook.
create or replace view public.period_index
with (security_invoker = on) as
select
  s.period_month                                        as period_month,
  min(s.group_date)                                     as first_sale,
  max(s.group_date)                                     as last_sale,
  count(*)                                              as statement_count,
  count(distinct s.consignment_id)                      as consignment_count,
  count(distinct s.product)                             as product_count,
  coalesce(sum(s.cartons_sold), 0)                      as cartons_sold,
  coalesce(sum(s.cartons_returned), 0)                  as cartons_returned,
  coalesce(sum(s.cartons_sold * s.price), 0)            as sales_value,
  max(s.created_at)                                     as last_recorded
from public.statements s
where s.period_month is not null
group by s.period_month;

comment on view public.period_index is
  'One row per month of recorded sales, so the months can be listed without reading the statements themselves. Honours the caller''s row-level security.';
