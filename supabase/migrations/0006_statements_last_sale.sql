-- Zacon: record when a consignment finished selling.
--
-- Run in the Supabase SQL editor after 0005. `date_received` already holds the
-- first sale date; this adds the last one, so the gap between them gives how
-- long a consignment took to sell. In real data that ranges from same-day to
-- three weeks, which is the main signal behind "what should I buy" advice.
--
-- Nullable: the account-sales format doesn't carry it, and rows recorded
-- before this migration have none.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

alter table public.statements add column if not exists last_sale date;
