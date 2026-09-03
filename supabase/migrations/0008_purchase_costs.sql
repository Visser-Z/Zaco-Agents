-- Zacon: what Zaco PAID for produce.
--
-- Run in the Supabase SQL editor after 0007. This is the missing half of the
-- picture. Every report the markets produce covers the selling side only --
-- what sold, for how much, and what the agent paid over. What Zaco paid its
-- own suppliers is a private transaction that appears in no market export, so
-- it cannot be derived from anything the system already reads. It has to be
-- entered once per delivery and product.
--
-- Keyed on (supplier_ref, product) because that is the unit produce is bought
-- in: one delivery from one supplier, of one line. A month is roughly fifteen
-- entries.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

create table if not exists public.purchase_costs (
  id              bigint generated always as identity primary key,
  supplier_ref    bigint not null,
  product         text   not null,
  cost_per_carton numeric(12, 2) not null,
  supplier        text,
  note            text,
  created_by      uuid references public.profiles(id) on delete set null,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),

  constraint purchase_costs_unique unique (supplier_ref, product)
);

comment on table public.purchase_costs is
  'What Zaco paid per carton for a given delivery and product. The only source of cost in the system.';

create index if not exists purchase_costs_supplier_idx
  on public.purchase_costs (supplier_ref);

alter table public.purchase_costs enable row level security;

create policy "purchase costs are readable by authenticated users"
  on public.purchase_costs for select to authenticated using (true);

create policy "authenticated users may record purchase costs"
  on public.purchase_costs for insert to authenticated
  with check (auth.uid() is not null);

create policy "authenticated users may correct purchase costs"
  on public.purchase_costs for update to authenticated
  using (auth.uid() is not null) with check (auth.uid() is not null);

create policy "authenticated users may remove purchase costs"
  on public.purchase_costs for delete to authenticated
  using (auth.uid() is not null);
