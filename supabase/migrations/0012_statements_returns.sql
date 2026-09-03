-- Zacon: keep returns as their own figures instead of only their net effect.
--
-- Run in the Supabase SQL editor after 0011. A negative docket on the market
-- report is a RETURN -- fruit back on the floor, reversing a sale that was
-- already booked -- and both parsers simply added it to the total. The net is
-- the right figure for the workbook (column J feeds K = H - J, so the stock
-- only balances if what came back is off the sold figure), but recorded as a
-- net alone the sale and the return vanish into each other: a month that sold
-- 3,448 cartons and had 280 come back reads identically to one that quietly
-- sold 3,168, and nothing in the history can tell them apart afterwards.
--
-- Both columns are held POSITIVE: "280 returned", never "-280".
--
-- Nullable, with no default. NULL means the source could not tell -- the
-- account-sales PDF, a row read back out of a workbook, or any row recorded
-- before this migration -- which is not the same as a report that showed the
-- figure and it was zero. Defaulting to 0 would claim the old history had no
-- returns, when the truth is that it never looked.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

alter table public.statements add column if not exists cartons_returned integer;

alter table public.statements add column if not exists returns_total numeric;
