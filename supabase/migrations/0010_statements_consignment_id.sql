-- Zacon: one row per account sale, so the statement number alone is no longer
-- unique.
--
-- Run in the Supabase SQL editor after 0009. A consignment sits on the market
-- floor and is sold off over several account sales, and one account sale
-- settles several consignments. The workbook is kept one row per account sale
-- per consignment, so identity is the pair, not the account sale on its own.
--
-- Without this, saving a statement that covered two products would record only
-- the first: the second would collide on statements_unique_per_agent and be
-- dropped by the ignore-duplicates upsert, silently.
--
-- 0 stands for "no consignment id" (the PDF exports do not print one). A real
-- NULL will not do: Postgres treats every NULL as distinct, so the unique key
-- would stop guarding against a row being recorded twice.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

alter table public.statements
  add column if not exists consignment_id bigint not null default 0;

alter table public.statements
  drop constraint if exists statements_unique_per_agent;

alter table public.statements
  add constraint statements_unique_per_agent
  unique (market_agent, stm_no, consignment_id);

create index if not exists statements_consignment_idx
  on public.statements (consignment_id);
