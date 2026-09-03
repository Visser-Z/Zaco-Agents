-- Zacon: initial schema, roles and row level security.
--
-- Run this in the Supabase SQL editor before any data exists. RLS is defined
-- here rather than added later because retrofitting it onto a populated ERP
-- means auditing every existing row and every query that already assumes
-- unrestricted access.
--
-- IMPORTANT for the backend: FastAPI must talk to Supabase using the *caller's*
-- JWT, not the service_role key. service_role bypasses RLS entirely, which
-- would make everything below decorative. Use service_role only for genuine
-- admin tasks (inviting users), never for ordinary requests.

-- ---------------------------------------------------------------- roles

create type public.user_role as enum ('admin', 'staff');

create table public.profiles (
  id         uuid primary key references auth.users on delete cascade,
  email      text not null,
  full_name  text,
  role       public.user_role not null default 'staff',
  created_at timestamptz not null default now()
);

comment on table public.profiles is
  'One row per authenticated user. Mirrors auth.users and carries the app role.';

-- A new signup gets a profile automatically, defaulting to the least
-- privileged role. Promotion to admin is a deliberate act, never a default.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, email, full_name)
  values (new.id, new.email, new.raw_user_meta_data ->> 'full_name');
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- Role lookup used by policies on other tables.
--
-- SECURITY DEFINER is essential: a policy that reads public.profiles while
-- profiles itself has RLS enabled would recurse infinitely. Running as the
-- definer sidesteps the caller's policies for this one narrow lookup.
create or replace function public.is_admin()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(
    (select role from public.profiles where id = auth.uid()) = 'admin',
    false
  );
$$;

-- ------------------------------------------------------- profiles policies

alter table public.profiles enable row level security;

-- A small internal team needs to see who did what, so profiles are readable
-- by any signed-in user. They are not readable anonymously.
create policy "profiles are readable by authenticated users"
  on public.profiles for select
  to authenticated
  using (true);

create policy "users may update their own profile"
  on public.profiles for update
  to authenticated
  using (id = auth.uid())
  with check (id = auth.uid());

create policy "admins may do anything to profiles"
  on public.profiles for all
  to authenticated
  using (public.is_admin())
  with check (public.is_admin());

-- The self-update policy above would otherwise let a user set their own role
-- to 'admin'. Column-level restriction is clearer as a trigger than as a
-- policy predicate.
create or replace function public.prevent_role_escalation()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.role is distinct from old.role and not public.is_admin() then
    raise exception 'Only an admin may change a user role';
  end if;
  return new;
end;
$$;

create trigger profiles_block_self_promotion
  before update on public.profiles
  for each row execute function public.prevent_role_escalation();

-- --------------------------------------------------------- product codes

-- Replaces backend/data/description_lookup.json. A table rather than a file so
-- the mapping is shared across users and survives redeployment.
create table public.product_codes (
  product    text primary key,   -- raw PDF PRODUCT string, whitespace-collapsed, uppercased
  code       text not null,      -- short code written to the Description column
  created_by uuid references public.profiles(id) on delete set null,
  created_at timestamptz not null default now()
);

comment on column public.product_codes.product is
  'Normalised PDF product string, e.g. "NEOT 1L MA50 36 T2 NECTARINE OTHER".';

alter table public.product_codes enable row level security;

create policy "product codes are readable by authenticated users"
  on public.product_codes for select
  to authenticated
  using (true);

-- Any signed-in user may map a product they encounter -- this is the normal
-- flow on the review screen, and blocking it would stall their work.
create policy "authenticated users may add product codes"
  on public.product_codes for insert
  to authenticated
  with check (auth.uid() is not null);

-- Changing or removing an existing mapping silently alters what lands in the
-- workbook, so those are admin-only.
create policy "admins may update product codes"
  on public.product_codes for update
  to authenticated
  using (public.is_admin())
  with check (public.is_admin());

create policy "admins may delete product codes"
  on public.product_codes for delete
  to authenticated
  using (public.is_admin());

-- ------------------------------------------------------------- statements

-- A record of every statement processed. The Excel workbook remains the
-- operator's working document; this is the durable history behind it, and the
-- foundation for moving off Excel as the ERP grows.
create table public.statements (
  id            bigint generated always as identity primary key,
  market_agent  text   not null,
  stm_no        bigint not null,           -- ACCOUNT SALES NO
  dn            bigint,                    -- REFNO
  product       text,
  description   text,
  qty_received  integer,
  opening_stock integer,
  cartons_sold  integer,
  price         numeric(12, 4),
  nett_total    numeric(14, 2),
  date_received date,
  invoice_date  date,
  group_date    date,                      -- column D: earliest received in the DN group
  status        text,                      -- column T: invoice date as DD.MM
  source_file   text,
  created_by    uuid references public.profiles(id) on delete set null,
  created_at    timestamptz not null default now(),

  -- Guards against the same statement being appended to the workbook twice.
  -- Scoped to the agent because account-sales numbering restarts per agent.
  constraint statements_unique_per_agent unique (market_agent, stm_no)
);

create index statements_dn_idx on public.statements (dn);
create index statements_created_at_idx on public.statements (created_at desc);

alter table public.statements enable row level security;

create policy "statements are readable by authenticated users"
  on public.statements for select
  to authenticated
  using (true);

create policy "authenticated users may record statements"
  on public.statements for insert
  to authenticated
  with check (auth.uid() is not null);

-- Processed statements are a financial record. Correcting or removing one is
-- an admin action, and staff mistakes are fixed by an admin rather than
-- silently overwritten.
create policy "admins may update statements"
  on public.statements for update
  to authenticated
  using (public.is_admin())
  with check (public.is_admin());

create policy "admins may delete statements"
  on public.statements for delete
  to authenticated
  using (public.is_admin());

-- ------------------------------------------------------------------ seed

-- The one product mapping already known from the sample statement.
insert into public.product_codes (product, code)
values ('NEOT 1L MA50 36 T2 NECTARINE OTHER', 'IMP Nect')
on conflict (product) do nothing;
