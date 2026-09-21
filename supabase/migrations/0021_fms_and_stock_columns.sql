-- Zacon: the identifiers that actually tie a payment to a sale, and the
-- market's own stock figures.
--
-- Run in the Supabase SQL editor after 0020. Additive only: three nullable
-- columns and one index. Nothing is rewritten and nothing is deleted.
--
-- payments.fms_id
--   The FMS id printed on every account sale. System-generated, one per
--   delivery, and the same on every account sale that delivery is ever paid
--   on. Supplier Ref, which matching used until now, is operator-entered free
--   text: 20 refs in this book cover more than one delivery, and some are a
--   date or the single letter N. The parser has always read the FMS id and
--   thrown it away. The payment's line numbers need no column: they travel in
--   the lines JSON already stored on the row.
--
-- statements.qty_amended
--   "Qty Amended To" on the Daily Sales report. The payment report's Delivered
--   column reports this figure, not Qty Sent, so it is both the true stock
--   basis and a matching check. Older exports leave it blank; the app then
--   uses Qty Sent.
--
-- statements.qty_avail
--   "Qty Avail" as the report printed it: what was still on the floor when
--   the report was run. A snapshot per consignment, so the latest report's
--   figure is the stock on hand. It used to be written into opening_stock and
--   then overwritten by the running balance; the two are different figures
--   and now each has its own column.
--
-- A re-dropped report fills these in on rows already saved: saving now
-- refreshes a row it has seen before instead of keeping the first copy.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

alter table public.payments
  add column if not exists fms_id text;

create index if not exists payments_fms_id_idx
  on public.payments (fms_id);

alter table public.statements
  add column if not exists qty_amended integer;

alter table public.statements
  add column if not exists qty_avail integer;
