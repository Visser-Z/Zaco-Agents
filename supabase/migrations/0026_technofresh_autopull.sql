-- Zacon: pull Payment Details and Daily Sales from the TechnoFresh portal by itself.
--
-- Run in the Supabase SQL editor after 0025. Two tables:
--
-- technofresh_settings  one row: the portal login and the schedule. The
--                       password is encrypted by the backend before it gets
--                       here (key in the TECHNOFRESH_KEY environment variable,
--                       never in the database), so reading this row gives
--                       ciphertext only. Every signed-in user may read it,
--                       because a manual "Pull now" and the 8am job both need
--                       it; only an admin may change it.
--
-- technofresh_days      one row per report per day pulled: what was found,
--                       saved, held for review and still unpaid, and the CSV
--                       itself so held rows can be opened in the review screen.
--                       A day's sales are pulled again while some of them still
--                       have no account sale number, because the market only
--                       assigns those a few days after the sale.

create table if not exists public.technofresh_settings (
  id            integer primary key default 1 check (id = 1),
  username      text,
  password_enc  text,
  schedule      text not null default 'daily' check (schedule in ('daily', 'weekly')),
  enabled       boolean not null default true,
  updated_by    uuid references public.profiles(id) on delete set null,
  updated_at    timestamptz not null default now()
);

comment on table public.technofresh_settings is
  'TechnoFresh portal login (password encrypted by the backend) and the auto-pull schedule.';

alter table public.technofresh_settings enable row level security;

create policy technofresh_settings_read on public.technofresh_settings for select
  to authenticated using (true);
create policy technofresh_settings_insert on public.technofresh_settings for insert
  to authenticated with check (public.is_admin());
create policy technofresh_settings_update on public.technofresh_settings for update
  to authenticated using (public.is_admin()) with check (public.is_admin());

create table if not exists public.technofresh_days (
  report      text not null check (report in ('sales', 'payments')),
  day         date not null,
  status      text not null check (status in ('done', 'waiting', 'review', 'failed')),
  found       integer not null default 0,
  saved       integer not null default 0,
  held        integer not null default 0,
  unpaid      integer not null default 0,
  message     text,
  csv         text,
  pulled_by   uuid references public.profiles(id) on delete set null,
  pulled_at   timestamptz not null default now(),
  primary key (report, day)
);

comment on table public.technofresh_days is
  'What each automatic TechnoFresh pull found, saved and held back, per report per day.';

alter table public.technofresh_days enable row level security;

create policy technofresh_days_read on public.technofresh_days for select
  to authenticated using (true);
create policy technofresh_days_insert on public.technofresh_days for insert
  to authenticated with check (auth.uid() is not null);
create policy technofresh_days_update on public.technofresh_days for update
  to authenticated using (auth.uid() is not null) with check (auth.uid() is not null);
create policy technofresh_days_delete on public.technofresh_days for delete
  to authenticated using (auth.uid() is not null);
