-- Zacon: columns the Nett reconciliation needs on the statements history.
--
-- Run this in the Supabase SQL editor after 0002. Daily Sales rows accumulate
-- here; a Payment Details report is later reconciled against them by
-- Supplier Ref + Product across its date range, and the Nett flows back.
--
--   supplier_ref -- the DN the payment documents key on (shared join field).
--   sales_total  -- the exact consignment sales value, compared to the payment
--                   Gross so the match is to the cent.
--
-- Both nullable: the account-sales format and older Daily Sales exports don't
-- carry them, and rows recorded before this migration have none.

alter table public.statements
  add column if not exists supplier_ref bigint;

alter table public.statements
  add column if not exists sales_total numeric(14, 2);

comment on column public.statements.supplier_ref is
  'Supplier Ref / DN shared with Payment Details, the Nett reconciliation key.';
comment on column public.statements.sales_total is
  'Exact consignment sales value, reconciled against Payment Details Gross.';

-- Reconciliation reads by supplier_ref within a date window.
create index if not exists statements_supplier_ref_idx on public.statements (supplier_ref);
