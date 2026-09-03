-- Zacon: the consignment model.
--
-- Run in the Supabase SQL editor after 0008.
--
-- Zaco does not BUY produce. Suppliers hand it over on consignment and are paid
-- only once it sells; Zaco's earnings are a commission percentage of what the
-- market returns. Everything below the Nett line is therefore money Zaco is
-- holding on someone else's behalf, not income -- and none of it exists in any
-- TechnoFresh export, because the market agents only ever see Zaco (producer
-- code 20026) as the supplier. The farmers behind Zaco are invisible to them.
--
-- Two tables:
--   suppliers          who the produce actually belongs to
--   consignment_deals  the terms and settlement state, per delivery line
--
-- `consignment_deals` is keyed on (supplier_ref, product) -- the same unit the
-- market reports and the reconciliation already use -- because the commission
-- is agreed PER DEAL, not per supplier. Roughly fifteen rows a month.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

create table if not exists public.suppliers (
  id                     bigint generated always as identity primary key,
  name                   text not null,
  contact                text,
  -- Falls back to this when a deal does not state its own rate. Stored as a
  -- percentage (30 = 30%), of the NETT that reaches Zaco.
  default_commission_pct numeric(5, 2) not null default 30,
  note                   text,
  created_by             uuid references public.profiles(id) on delete set null,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now(),

  constraint suppliers_name_unique unique (name),
  constraint suppliers_pct_sane check (default_commission_pct >= 0 and default_commission_pct <= 100)
);

comment on table public.suppliers is
  'Whose produce it actually is. Absent from every market export, so captured here.';

create table if not exists public.consignment_deals (
  id             bigint generated always as identity primary key,
  supplier_ref   bigint not null,
  product        text   not null,
  supplier_id    bigint references public.suppliers(id) on delete set null,
  -- Zaco's cut, as a percentage of the Nett that lands from the market agent.
  -- Per deal, because the rate is negotiated per consignment.
  commission_pct numeric(5, 2),
  -- Settlement state. `settled_at` is when the supplier was actually paid;
  -- `settled_amount` is what they were paid, which may differ from the computed
  -- figure (a rounding, an advance, a deduction agreed between them).
  settled_at     timestamptz,
  settled_amount numeric(12, 2),
  note           text,
  created_by     uuid references public.profiles(id) on delete set null,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),

  constraint consignment_deals_unique unique (supplier_ref, product),
  constraint consignment_deals_pct_sane check (commission_pct is null
    or (commission_pct >= 0 and commission_pct <= 100))
);

comment on table public.consignment_deals is
  'Terms and settlement for one delivery line taken on consignment: whose it is, Zaco''s commission percentage of Nett, and whether the supplier has been paid.';

create index if not exists consignment_deals_ref_idx
  on public.consignment_deals (supplier_ref);
create index if not exists consignment_deals_supplier_idx
  on public.consignment_deals (supplier_id);
-- Finding what is still owed is the most common question this table answers.
create index if not exists consignment_deals_unsettled_idx
  on public.consignment_deals (settled_at) where settled_at is null;

alter table public.suppliers enable row level security;
alter table public.consignment_deals enable row level security;

create policy "suppliers are readable by authenticated users"
  on public.suppliers for select to authenticated using (true);
create policy "authenticated users may add suppliers"
  on public.suppliers for insert to authenticated with check (auth.uid() is not null);
create policy "authenticated users may correct suppliers"
  on public.suppliers for update to authenticated
  using (auth.uid() is not null) with check (auth.uid() is not null);
create policy "authenticated users may remove suppliers"
  on public.suppliers for delete to authenticated using (auth.uid() is not null);

create policy "deals are readable by authenticated users"
  on public.consignment_deals for select to authenticated using (true);
create policy "authenticated users may record deals"
  on public.consignment_deals for insert to authenticated with check (auth.uid() is not null);
create policy "authenticated users may correct deals"
  on public.consignment_deals for update to authenticated
  using (auth.uid() is not null) with check (auth.uid() is not null);
create policy "authenticated users may remove deals"
  on public.consignment_deals for delete to authenticated using (auth.uid() is not null);
