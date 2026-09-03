-- Zacon: record which payment each sale was paid under.
--
-- Run in the Supabase SQL editor after 0006. The CSV export of the Daily Sales
-- report names, on every docket, the AccSale number that docket was paid
-- under. That is a direct join between a sale and its payment, replacing the
-- old approach of matching on supplier ref + product name + value.
--
-- Stored as "REF=value;REF=value" because a consignment sold over several days
-- is paid across several runs, and each run reconciles on its own.
--
-- Nullable: the PDF exports do not carry it, and rows recorded before this
-- migration have none -- those still fall back to product-name matching.
--
-- Paste WITHOUT the comment lines if the editor mangles the leading dashes.

alter table public.statements add column if not exists payment_refs text;
