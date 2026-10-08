-- Zacon: the TechnoFresh auto-pull fetches PDFs, not CSVs.
--
-- Run in the Supabase SQL editor after 0026. The PDFs are what the app has
-- read reliably for months, and a PDF sales row is filed under its Consignment
-- ID, so it goes on the book the day it sold instead of waiting days for an
-- account sale number. Each pulled day's PDF is kept (base64) so rows held for
-- a short code open in the review screen. The csv column from 0026 is left in
-- place, unused, rather than dropped.

alter table public.technofresh_days add column if not exists pdf text;

comment on column public.technofresh_days.pdf is
  'The pulled report PDF, base64, for reopening held rows in the review screen.';
