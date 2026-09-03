-- Zacon: one row per consignment per TRADING DAY.
--
-- Run in the Supabase SQL editor after 0013. Read the whole of this comment
-- before running it; it changes what counts as a duplicate.
--
-- The reports are now loaded a trading day at a time. The Daily Sales Detail
-- PDF prints no account sale number, so column E carries the Consignment ID
-- instead, and a consignment that sells over several days produces one row per
-- day, all of them carrying that same Consignment ID.
--
-- Under the old key (market_agent, stm_no, consignment_id) those rows are the
-- same row. The insert ignores duplicates, so the first day was recorded and
-- every later day was dropped without a word. Measured on eight real August
-- reports: 60 rows collapsed to 28 keys, 32 rows silently discarded, and 752 of
-- 1 213 cartons lost. On one consignment the row that survived was a
-- returns-only day of MINUS 2 cartons while the 37-carton day was thrown away.
--
-- The trading day is therefore part of identity. It also leaves the account
-- sales path alone: there, one row is one (consignment, account sale) and the
-- triple was already unique, so adding the date cannot split anything that
-- belonged together.
--
-- group_date is NOT NULL for this to bite. Rows that never had one are given
-- the date they were recorded, which is the only date they carry.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

update public.statements
   set group_date = created_at::date
 where group_date is null;

alter table public.statements
  alter column group_date set not null;

alter table public.statements
  drop constraint if exists statements_unique_per_agent;

alter table public.statements
  add constraint statements_unique_per_agent
  unique (market_agent, stm_no, consignment_id, group_date);
