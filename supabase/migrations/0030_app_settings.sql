-- Zacon: settings for the whole business, one row.
--
-- Run after 0029. Starts with the price check: how many days after a
-- consignment first sells a sale under the market average is flagged. After
-- that the agent is clearing old stock, which is meant to go under the average.
-- Every signed-in user reads it (Insights needs it); only an admin changes it.
-- Later settings (the morning message, say) become columns here.

create table if not exists public.app_settings (
  id                 integer primary key default 1 check (id = 1),
  price_check_days   integer not null default 5 check (price_check_days between 1 and 30),
  updated_by         uuid references public.profiles(id) on delete set null,
  updated_at         timestamptz not null default now()
);

comment on table public.app_settings is
  'Business wide settings, one row. price_check_days: days after first sale that a sale under the market average is flagged.';

alter table public.app_settings enable row level security;

create policy app_settings_read on public.app_settings for select
  to authenticated using (true);
create policy app_settings_insert on public.app_settings for insert
  to authenticated with check (public.is_admin());
create policy app_settings_update on public.app_settings for update
  to authenticated using (public.is_admin()) with check (public.is_admin());

grant select, insert, update on public.app_settings to authenticated;

insert into public.app_settings (id) values (1) on conflict (id) do nothing;
