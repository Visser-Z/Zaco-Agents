-- Zacon: one row per consignment per DAY IT SOLD, not per delivery date.
--
-- Run in the Supabase SQL editor after 0019. This fixes silent data loss.
--
-- The unique key was (market_agent, stm_no, consignment_id, group_date), and
-- group_date is the consignment's date, not the sale's: apply_group_dates sets
-- it to the earliest received date across the delivery-note group. Drop one
-- day's report and that is the day. Drop a week of reports together and every
-- row of a consignment collapses onto ONE group_date, so the second day
-- onwards collide with the first and the insert, which ignores duplicates,
-- discards them without a word.
--
-- Measured on this database: of 30 September sales in the reports, 14 reached
-- the book. R78 591,95 of a single month was dropped on the floor. Only the
-- first day each consignment traded survived.
--
-- sale_day is the day the row sold, taken from the sale date and falling back
-- down the same chain the app dates a row by. It is GENERATED, so it cannot
-- drift from the columns it comes from, and NULLS NOT DISTINCT so undated rows
-- still collide rather than multiplying.
--
-- Safe to run more than once. It does not delete anything; rows lost earlier
-- have to be re-imported by dropping those reports again, which now works.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

alter table public.statements
  add column if not exists sale_day date
  generated always as (
    coalesce(last_sale, group_date, invoice_date, date_received)
  ) stored;

comment on column public.statements.sale_day is
  'The day this row sold. The grain of the book: one consignment selling on Monday and again on Wednesday is two rows, and this is what keeps them apart.';

create unique index if not exists statements_unique_per_sale_day
  on public.statements (market_agent, stm_no, consignment_id, sale_day)
  nulls not distinct;

-- 0014 created this as a constraint, so it is dropped as one; the index
-- behind it goes with it.
alter table public.statements drop constraint if exists statements_unique_per_agent;
drop index if exists statements_unique_per_agent;
