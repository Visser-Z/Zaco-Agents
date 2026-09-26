-- Zacon: keep the conversations with the assistant.
--
-- Run in the Supabase SQL editor after 0021. Until now a conversation lived in
-- the browser tab: closing it, reloading, or opening the app on the phone lost
-- every question and every answer. That made the assistant something you
-- consulted rather than something you worked with, because nothing could be
-- picked up where it was left.
--
-- Two tables. A thread is one conversation, named after its first question. A
-- message is one turn in it, question or answer, in the order it happened.
-- Answers keep the analyst findings that came with them so a reopened thread
-- looks exactly as it did when it was live.
--
-- A conversation is the person's own working notes, not a financial record:
-- unlike every other table here it is private to whoever started it, so the
-- policies compare created_by against auth.uid() rather than letting the whole
-- team read it. Deleting a thread takes its messages with it.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

create table if not exists public.chat_threads (
  id           uuid primary key default gen_random_uuid(),
  title        text not null default 'New conversation',
  created_by   uuid references public.profiles(id) on delete cascade,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

comment on table public.chat_threads is
  'One conversation with the assistant, private to whoever started it.';

create table if not exists public.chat_messages (
  id           bigint generated always as identity primary key,
  thread_id    uuid not null references public.chat_threads(id) on delete cascade,
  role         text not null check (role in ('q', 'a')),
  body         text not null,
  findings     jsonb not null default '[]'::jsonb,
  failed       boolean not null default false,
  created_by   uuid references public.profiles(id) on delete cascade,
  created_at   timestamptz not null default now()
);

comment on column public.chat_messages.role is
  'q for what was asked, a for what came back. Same two letters the screen uses.';

-- A thread is read newest first; its messages oldest first.
create index if not exists chat_threads_mine on public.chat_threads (created_by, updated_at desc);
create index if not exists chat_messages_thread on public.chat_messages (thread_id, id);

alter table public.chat_threads enable row level security;
alter table public.chat_messages enable row level security;

create policy chat_threads_own
  on public.chat_threads for all
  to authenticated
  using (created_by = auth.uid())
  with check (created_by = auth.uid());

-- Messages are reachable only through a thread the caller owns, so the check
-- goes through the parent rather than trusting the column on the row.
create policy chat_messages_own
  on public.chat_messages for all
  to authenticated
  using (exists (select 1 from public.chat_threads t
                 where t.id = thread_id and t.created_by = auth.uid()))
  with check (exists (select 1 from public.chat_threads t
                      where t.id = thread_id and t.created_by = auth.uid()));
