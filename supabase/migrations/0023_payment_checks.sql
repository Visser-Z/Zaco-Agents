-- Zacon: let the operator check a payment the system could not be sure of.
--
-- Run in the Supabase SQL editor after 0022. The app matches payments to sales
-- on its own, by FMS id where the payment carries one and by delivery note and
-- product otherwise. Most of it is certain. Some is not: a payment dated before
-- the sale it landed on had even started selling, a payment whose delivery
-- note has no sales on the book, a payment whose carton count belongs to a
-- different delivery. Those are flagged on the Tracking tab, and this table
-- holds what a person decided about each one.
--
-- One row per payment line, named the way the market names it: the account
-- sale and the product. The decision is either
--
--   keep   the system put it in the right place; stop flagging it
--   link   it belongs to this consignment instead; match it there from now on
--
-- A link is honoured everywhere a payment is matched, because it is put onto
-- the payment line as the payments are read. Undoing a decision is deleting
-- the row, and the payment goes back to being matched by the rules.
--
-- Shared by the whole team, like closed Tracking lines: it is a working record
-- of who checked what, not a financial one, and nothing in the sales or payment
-- history is changed by it.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

create table if not exists public.payment_checks (
  accsale         text not null,
  product         text not null,
  decision        text not null check (decision in ('keep', 'link')),
  consignment_id  bigint,
  note            text,
  created_by      uuid references public.profiles(id) on delete set null,
  created_at      timestamptz not null default now(),
  primary key (accsale, product),
  -- A link without somewhere to go is not a decision.
  check (decision <> 'link' or consignment_id is not null)
);

comment on table public.payment_checks is
  'What a person decided about a payment the matcher could not be sure of: keep where it was put, or link it to a named consignment.';

alter table public.payment_checks enable row level security;

create policy payment_checks_read
  on public.payment_checks for select
  to authenticated
  using (true);

create policy payment_checks_insert
  on public.payment_checks for insert
  to authenticated
  with check (auth.uid() is not null);

create policy payment_checks_update
  on public.payment_checks for update
  to authenticated
  using (auth.uid() is not null)
  with check (auth.uid() is not null);

create policy payment_checks_delete
  on public.payment_checks for delete
  to authenticated
  using (auth.uid() is not null);
