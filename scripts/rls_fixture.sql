create role anon nologin;
create role authenticated nologin;
create schema if not exists auth;

-- Supabase's auth.uid(), emulated: the signed-in user's id for this session.
create or replace function auth.uid() returns uuid language sql stable as $$
  select nullif(current_setting('test.uid', true), '')::uuid
$$;

create type public.user_role as enum ('admin','staff');

create table public.profiles (
  id uuid primary key,
  role public.user_role not null default 'staff'
);

create or replace function public.is_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select coalesce((select role from public.profiles where id = auth.uid()) = 'admin', false)
$$;

create table public.statements (
  id bigint generated always as identity primary key,
  market_agent text not null,
  group_date date,
  last_sale date,
  created_by uuid references public.profiles(id)
);
create table public.payments (
  accsale text primary key,
  paid_on date,
  created_by uuid references public.profiles(id)
);
create table public.product_codes (code text primary key, description text);

alter table public.statements enable row level security;
alter table public.payments enable row level security;
alter table public.product_codes enable row level security;

-- exactly the policy set the real database grew, admin-gated for write
create policy s_read on public.statements for select to authenticated using (true);
create policy s_ins  on public.statements for insert to authenticated with check (auth.uid() is not null);
create policy "admins may update statements" on public.statements for update to authenticated
  using (public.is_admin()) with check (public.is_admin());
create policy "admins may delete statements" on public.statements for delete to authenticated
  using (public.is_admin());
create policy "users may delete their own statements" on public.statements for delete to authenticated
  using (created_by = auth.uid() or public.is_admin());

create policy p_read on public.payments for select to authenticated using (true);
create policy payments_update on public.payments for update to authenticated
  using (public.is_admin()) with check (public.is_admin());
create policy payments_delete on public.payments for delete to authenticated
  using (public.is_admin());

create policy pc_read on public.product_codes for select to authenticated using (true);
create policy "admins may update product codes" on public.product_codes for update to authenticated
  using (public.is_admin()) with check (public.is_admin());
create policy "admins may delete product codes" on public.product_codes for delete to authenticated
  using (public.is_admin());

grant usage on schema public, auth to anon, authenticated;
grant all on all tables in schema public to anon, authenticated;
grant all on all sequences in schema public to anon, authenticated;

-- a staff operator, and rows recorded by SOMEONE ELSE (the real situation)
insert into public.profiles values ('11111111-1111-1111-1111-111111111111','staff');
insert into public.profiles values ('22222222-2222-2222-2222-222222222222','staff');
insert into public.statements (market_agent, group_date, last_sale, created_by)
  select 'Farmers Trust', date '2026-07-31', date '2026-08-03',
         '22222222-2222-2222-2222-222222222222' from generate_series(1,43);
insert into public.payments values ('PRE*BT*1', date '2026-08-05', '22222222-2222-2222-2222-222222222222');
