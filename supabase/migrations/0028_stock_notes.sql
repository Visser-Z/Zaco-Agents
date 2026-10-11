-- Zacon: Zaco's own delivery notes, the stock sent to each market.
--
-- Run in the Supabase SQL editor after 0027. The market's reports only show a
-- consignment once something on it has sold, so stock that has not sold yet is
-- invisible to them. Zaco's delivery note says what went on the truck: the DN
-- number (the same number the market prints as Supplier Ref), the date, where
-- it went, and each line as written ("Imp Pink grapes, 5kg, 54+13"). The app
-- holds each line against what the market booked and sold on that DN, and
-- that is the stock on the floor, the stock sold and not paid, and the paid.
--
-- Shared by the whole team: the note is the business's record of what left.

create table if not exists public.stock_notes (
  dn          bigint primary key,
  sent_on     date not null,
  market      text not null,
  agent       text,
  remarks     text,
  created_by  uuid references public.profiles(id) on delete set null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create table if not exists public.stock_note_lines (
  dn           bigint not null references public.stock_notes(dn) on delete cascade,
  line_no      integer not null,
  description  text not null,
  unit         text,
  written      text,
  cartons      integer not null check (cartons >= 0),
  -- A consignment a person tied this line to by hand, when the app could not.
  consignment_id bigint,
  primary key (dn, line_no)
);

comment on table public.stock_notes is
  'Zaco delivery notes: what was sent to which market, keyed by the DN number the market reports as Supplier Ref.';

alter table public.stock_notes enable row level security;
alter table public.stock_note_lines enable row level security;

create policy stock_notes_read on public.stock_notes for select to authenticated using (true);
create policy stock_notes_insert on public.stock_notes for insert to authenticated with check (auth.uid() is not null);
create policy stock_notes_update on public.stock_notes for update to authenticated using (auth.uid() is not null) with check (auth.uid() is not null);
create policy stock_notes_delete on public.stock_notes for delete to authenticated using (auth.uid() is not null);

create policy stock_note_lines_read on public.stock_note_lines for select to authenticated using (true);
create policy stock_note_lines_insert on public.stock_note_lines for insert to authenticated with check (auth.uid() is not null);
create policy stock_note_lines_update on public.stock_note_lines for update to authenticated using (auth.uid() is not null) with check (auth.uid() is not null);
create policy stock_note_lines_delete on public.stock_note_lines for delete to authenticated using (auth.uid() is not null);
