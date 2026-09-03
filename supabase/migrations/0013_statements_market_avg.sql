-- Zacon: keep what the market itself was paying, so the price can be checked.
--
-- Run in the Supabase SQL editor after 0012. The Daily Sales Detail report has
-- always had a Market Avg column, and every export up to July 2026 left it at
-- 0.00 in every line. That is the single reason the app has always said the
-- price a carton fetched cannot be verified. From August 2026 the agent fills
-- it in, so the question finally has an answer and the figure is worth keeping.
--
-- Nullable, no default. NULL means the report did not carry it, which is not
-- the same as a market average of zero. Rows recorded before this migration,
-- and every row from an export that left the column empty, keep NULL and are
-- excluded from the check rather than counted as having sold at nothing.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

alter table public.statements add column if not exists market_avg numeric;
