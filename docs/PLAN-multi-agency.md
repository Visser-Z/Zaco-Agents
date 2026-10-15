# Plan: turning ZacoAgents into a multi agency product

Status: planning only. No code has been changed. Written 15 October 2026 from the code at commit `0ff3c39`.

---

## 0. Read this first: one finding that changes the plan

The brief describes Zaco as a market agency that sells growers' produce on the market. **The code describes a different business.** In the code, Zaco is a consignment intermediary with its own **producer code 20026**. It takes fruit from growers, delivers it to *market agents* at several markets (Farmers Trust at Tshwane, Subtropico at Joburg and Springs, Grow Marco at Joburg, Grow Port Natal at Durban), and earns a commission on what those agents return. See `supabase/migrations/0009_consignment.sql` ("the market agents only ever see Zaco (producer code 20026) as the supplier") and `backend/app/payment_details.py:188` (`DESTINATIONS`).

Why it matters:

1. **The Techno Fresh reports Zaco pulls are the producer's view** (one producer's consignments across many agents and markets). A registered market agency sees the *agent's* view: many producers, one market, its own floor stock, its own trust account. The report layouts, the keys (supplier ref vs consignment per producer) and the business rules (who earns commission, who pays the grower) are not the same. Much of the parsing and matching code (`daily_sales.py`, `payment_details.py`, `tracking.py`, `reconcile.py`) is built for the producer's view.
2. **Market agents such as Farmers Trust and Subtropico are Zaco's own trading partners.** Selling this product to them is commercially sensitive for Zaco, and possibly a "direct competitor" question in itself.
3. Businesses shaped like Zaco (consignment intermediaries, export agents, distributors) are actually the *closest fit* to today's code. Those are the "later" customers in the brief.

The tenancy, onboarding, module toggles and isolation work is identical for both kinds of customer. What differs is how much of the report parsing and payment matching has to be built again for the agent's view.

### Decision (15 October 2026)

Confirmed: the code is right. Zaco is a consignment intermediary with producer code 20026 that sends to market agents.

**Customers are sold to in this order:**

1. **Segment A, first: businesses shaped like Zaco.** Consignment intermediaries, packhouses, export and distribution agents that deliver to market agents and see the producer's view in Techno Fresh. The current report readers, payment matching, Tracking, Stock and Procurement are reused. The main change is replacing the hardcoded 20026 and market lists with per agency settings. The first agency comes through an introduction the owner already has.
2. **Segment B, later: market agents** (the people on the market side) and **the supplier side** (growers, packhouses that want their own view). These need agent side report readers, trust account reporting and Zaco's explicit agreement, because some market agents are Zaco's trading partners. They are planned as a separate stage after segment A is live and paying (section 5, "Later").

Phases 1 to 3 and 11 below are written for segment A. Phases 4 to 7 (grower portal, WhatsApp, statements, compliance) serve both, but the trust account and APAC audit parts of Phase 7 matter mainly for segment B and move later.

Throughout this document, **agency** means a paying customer (a tenant), **grower** means whoever owns the produce on consignment, and **market** means a fresh produce market. Today's `suppliers` table is what this plan calls growers.

---

## 1. What exists today

### 1.1 Architecture

| Part | What it is |
|---|---|
| Backend | FastAPI, Python 3.14, `backend/app` (about 15 000 lines, 33 modules). `main.py` alone is 2 709 lines and holds all 45 routes. |
| Frontend | One file, `frontend/index.html` (7 240 lines), vanilla JS, no build step. Brand colours as CSS tokens, logo embedded as base64. Tabs: Sales and Payments, Tracking, Insights, Stock, Procurement. |
| Desktop | Electron wrapper (`desktop/`) that opens `https://zacodasboard.co.za`, built as `ZacoAgents.exe`. |
| Hosting | Vercel serverless, one Python function (`api/index.py`) with a 60 second limit. Deploy on push to `main`. `DEPLOY.md` still describes an older VPS plus Tailscale setup. |
| Database | One Supabase project (`bixkmdvcxbwicccckodt`), Postgres plus Supabase Auth. 29 migrations, all applied to production. |
| AI | Claude Haiku via `assistant.py` (chat, procurement plan, supply write up, document checker). Prompts name Zaco and its markets. |
| Tests | 50 pytest files, about 640 tests, fixtures built from Zaco's real reports. |

### 1.2 Data model (19 tables)

Core book: `statements` (sales per consignment per day), `payments`, `payment_checks`, `dismissals`, `market_summaries`, `delivery_notes`, `stock_notes`, `stock_note_lines`, `stock_carryovers`, `market_stock`, `period_blocks` (via policies).
Growers and deals: `suppliers`, `consignment_deals`, `purchase_costs`.
Reference: `product_codes` (global product to short code map).
Sync: `technofresh_settings` (a **single row**, `id = 1`), `technofresh_days` (keyed on `(report, day)`).
Assistant: `chat_threads`, `chat_messages`.
Users: `profiles` (role `admin` or `staff`).

Things that assume one business:

* `technofresh_settings` is forced to one row (`check (id = 1)`).
* Unique keys are global: `product_codes.product`, `suppliers.name`, `statements (market_agent, stm_no)`, `consignment_deals (supplier_ref, product)`, `technofresh_days (report, day)`.
* No table has an owner column other than `created_by`.

### 1.3 Auth and access control

* Supabase Auth, email and password. The backend verifies the JWT (`supabase_auth.py`) and forwards the **caller's own token** to PostgREST, so RLS is evaluated as the user. The service role key is never used. This is a very good starting point for multi tenancy.
* **But every RLS policy today is "any signed in user may read and write everything"** (`using (true)` or `auth.uid() is not null`, see `0001_init.sql` and `0019_no_admin_gate.sql`). Admin only applies to changing roles and Techno Fresh settings. If a second agency's user were created today, they would see all of Zaco's data. **Nothing may be onboarded until Phase 1 is done.**
* `handle_new_user` creates a profile for any new auth user. Whether public sign up is switched off in the Supabase dashboard needs checking (Q11).
* `/api/health` is open and returns the Supabase URL and anon key (fine, they are public by design).

### 1.4 How the Techno Fresh pull works (`technofresh.py`, `main.py:2440` onward)

* Portal `crm.technofresh.co.za` has no API. The code signs in by posting the login form (one `PHPSESSID` cookie), then posts the report forms: Payment Details (report 24, PDF), Daily Sales Detail (report 8, PDF), Stock On Hand (report 28, CSV, once per market code).
* PDFs are parsed by the same readers used for manual uploads (`daily_sales.py`, `payment_details.py`); stock by `market_stock.py`.
* Vercel Cron calls `/api/technofresh/cron` at 06:00 UTC (08:00 SAST) with `CRON_SECRET`. The job signs in to Supabase as a **robot user** (`ZACON_ROBOT_EMAIL` / `ZACON_ROBOT_PASSWORD`) so writes still go through RLS.
* `days_to_pull` works out which days to fetch: yesterday, any missed days (up to 31 back), failed days, and sales days still waiting for an account sale number (up to 14 days). Requests are paced 2 seconds apart and the run stops starting new days after 45 seconds.
* The portal password is Fernet encrypted with `TECHNOFRESH_KEY` (one key in the environment). Each day's PDF is kept base64 in `technofresh_days.pdf` for the review screen.
* Manual upload of the same PDFs (and some CSV layouts in `csv_reports.py`) already exists, so a "fallback" partly exists for Techno Fresh layouts.

Limits for many agencies: one login, one robot, one key, one 60 second run. Ten agencies doing a 31 day catch up will not fit.

### 1.5 Where Zaco is hardcoded

| Kind | Where | Notes |
|---|---|---|
| Producer code 20026 | `reconcile.py:340`, `market_stock.py:58`, `delivery.py`, `nett_adjustments.py:94`, `schemas.py:98`, `daily_sales.py:60`, `payment_details.py:82` | Used to spot placeholder refs (`20026*20026`). Must become the agency's own producer code(s). |
| Market codes | `market_stock.MARKETS` (PRE, JOH, TFR, DUR, SPR, CAP) | Drives which stock reports are pulled. |
| Account sale prefix to market and agent | `payment_details.DESTINATIONS` | Five Zaco destinations. An unknown prefix is quarantined, which is the right failure mode, but every new agency would hit it. |
| Market and agent names | `csv_reports.py`, `daily_sales.py`, `stock_book.py`, `tracking.py`, `assistant.py`, `frontend/index.html` | Comments, regexes and some display logic. |
| Company name | `order_sheet.py:147,148,280,303`, `dispatch_sheet.py:51,190,208`, `stocktake_sheet.py:46,56`, PDF file names (`zaco-dispatch-...`, `main.py:1754,1824`) | Printed on documents. |
| AI prompts | `assistant.py:143-210, 1014, 1089` | System prompts describe Zaco's business and markets. |
| Branding | `frontend/index.html` (title, navy `#04235F`, gold `#CFB058`, base64 logo at line 84), `desktop/config.json`, `desktop/package.json` | |
| App name and domain | `main.py:50` (`FastAPI(title="ZacoAgents")`), `technofresh.py` User-Agent, `zacodasboard.co.za` | |
| Business rules | Default commission 30% (`0009_consignment.sql`), procurement weights (`procurement.py:67`), 30 day old debt warning (`dispatch.py:41`), unpaid colour bands 14/30 days (Tracking), 08:00 pull, "no purchase price" assumption everywhere | Should become per agency settings with today's values as defaults. |
| Env names | `ZACON_*`, single `TECHNOFRESH_KEY`, single robot | |
| Test fixtures | `backend/tests/fixtures/*.txt` | Real Zaco report text. Excluded from deploy (`.vercelignore`), but must stay out of anything shared with other agencies or contractors. |

---

## 2. Multi tenancy approach

### Options

| | Shared DB, `agency_id` on every row, RLS | Schema per agency | Separate Supabase project (DB) per agency |
|---|---|---|---|
| Isolation | Logical. Relies on RLS being right everywhere. | Better. A missed filter hits an empty schema, not another agency. | Strongest. Physically separate. |
| Fits current code | **Very well.** The app already goes through PostgREST with the user's JWT and RLS. Adding one predicate to every policy is the core change. | Poorly. PostgREST exposes fixed schemas; switching schema per request needs config per tenant or `search_path` tricks. Supabase Auth is shared anyway. | Moderately. One deployment per agency or a routing layer that picks the project per login. |
| Migrations | Run once. | Run N times, can drift. | Run N times, N projects, N sets of keys and backups. |
| Cost | One project. | One project. | One paid project per agency (roughly USD 25+/month each). |
| Cross agency reporting for you | Easy. | Moderate. | Hard. |
| Onboarding a new agency | Insert a row. | Create schema plus run migrations. | Create project, run migrations, set env, deploy. |
| Ops for a small team | Lowest. | Medium. | Highest. |

### Recommendation: shared database, `agency_id` plus RLS, with a safety net

For your size (a handful to a few dozen agencies, small team) the shared database is the simple, proven choice and it matches how the app already works. Make it safe with four rules:

1. **Every tenant table gets `agency_id uuid not null references agencies`.** Its default is `public.current_agency_id()`, so existing backend inserts keep working without passing the id.
2. **Every policy on every tenant table is `agency_id = public.current_agency_id()`**, generated by one script so none is missed. A CI check (extend `scripts/check_rls.sh`) fails the build if any table in `public` lacks RLS or lacks the agency predicate.
3. **Automated two agency isolation tests**: a fixture with agency A and agency B, run every endpoint as a user of B, assert no A rows ever come back and no A row can be written.
4. **Keep an escape hatch.** Because tables carry `agency_id`, a large or nervous customer can later be moved to their own Supabase project with a filtered dump. You can sell "dedicated database" as a Pro add on without a redesign.

**What about Zaco specifically?** Zaco is the one customer with a hard confidentiality ask. Option worth discussing (Q4): keep Zaco on a **separate Supabase project** and run every new agency on a new shared multi tenant project. That gives Zaco physical isolation and removes all migration risk for Zaco's live data on day one. The cost is two databases to migrate and back up. My default recommendation is still a single shared project with Zaco migrated in place (section 4, Phase 1), because the RLS work must be correct for every other agency anyway, and running two code paths for long is the bigger risk for a small team. But if Zaco's contract or comfort requires it, the separate project route is cheap to choose now and expensive to choose later.

### How a request knows its agency

* New tables `agencies` and `agency_members (agency_id, user_id, role)`.
* `public.current_agency_id()` is a `security definer` SQL function returning the caller's agency. Start with "one agency per user" (unique `user_id` in `agency_members`), which is all salesmen and office staff need. Users who belong to several agencies (you, as support) are handled in Phase 6 with an explicit "acting as" choice stored in the JWT via a Supabase custom access token hook.
* The backend changes very little: it already forwards the user's token. `current_profile` grows `agency_id`, `role` and `modules` so routes and the frontend can gate features.

---

## 3. Procurement lock and competitor rule (hard constraint)

Design so the rule **fails closed** and is enforced in the database, not only in the UI.

Data:

* `agencies.competitor_status` enum: `unclassified` (default for every new agency), `competitor`, `not_competitor`. Plus `competitor_reason text`, `classified_by`, `classified_at`.
* `platform_rules` table (owner only, not readable by agencies) holding the configurable definition, e.g. `{"same_market_as_zaco": true, "markets": ["PRE","JOH","TFR","DUR","SPR"], "explicit_agency_ids": [...], "same_producer_codes": true}`. A function `suggest_competitor_status(agency_id)` applies the rule to the agency's markets and codes and proposes a status. **A person confirms it.** The rule only suggests; it never silently clears an agency.
* `agency_modules (agency_id, module, enabled, enabled_by, enabled_at, note)`.

Enforcement, three layers:

1. **Database trigger** on `agency_modules`: refuses `module = 'procurement' and enabled` unless the agency's `competitor_status = 'not_competitor'`. Another trigger on `agencies` switches procurement off automatically if an agency is ever reclassified to `competitor` or `unclassified`, or if it adds a market that the rule covers.
2. **Backend**: a `require_module("procurement")` dependency on every `/api/procurement/*` route (8 routes in `main.py:1601-1743`) and on the assistant's plan and supply endpoints (`/api/assistant/plan`, supply brief). Returns 404, not 403, so the feature is not advertised.
3. **Frontend**: the Procurement tab is only rendered when `/api/me` lists the module.

**Segment A makes this rule bite early.** Zaco's direct competitors are, by definition, businesses like Zaco delivering to the same markets or market agents, which is exactly segment A. Expect several early customers to be classified `competitor`. They can still buy the product; they just never get procurement. Classify the first introduced agency before its contract is signed, so procurement is never promised to it by mistake.

Only the platform owner (you) can change `competitor_status`, `platform_rules` and `agency_modules`. Agency admins cannot. All changes go in the audit log.

Zaco's procurement know how lives in code (`procurement.py`, `dispatch.py`, `supply.py`, the AI prompts). Even with the module off, that code ships to every tenant's server, but tenants cannot reach it. Q3 asks whether Zaco is comfortable with *non*-competitors getting the same procurement logic, or whether Zaco's tuned settings (weights, thresholds) must stay Zaco only. The plan makes all tuning values per agency settings so non-competitors get neutral defaults, not Zaco's numbers.

---

## 4. Phases

### Phase 0. Move to Azure South Africa, changing nothing else (decided 15 October 2026)

**Why**: host every agency's data in South Africa. Today the database is Supabase project `bixkmdvcxbwicccckodt` in `eu-west-2` (London), and Supabase's hosted service has no South African region. The app runs on Vercel (US by default). Azure has South Africa North (Johannesburg) and South Africa West (Cape Town).

**Rule**: this is a like for like move. Same schema, same RLS policies, same code paths, same features. No multi tenancy changes in the same release. Phase 1 starts only after Zaco has run on Azure for at least a week without problems.

**Target, all in South Africa North, one resource group `rg-zacon-prod`:**

| Today | On Azure | Notes |
|---|---|---|
| Supabase Postgres 17 | Azure Database for PostgreSQL Flexible Server, Postgres 17 | Managed backups with point in time restore (set retention to 35 days), geo redundant backup to South Africa West if offered. |
| Supabase Auth (GoTrue) | `supabase/auth` container on Azure Container Apps | Same login API the frontend already calls, so the login screen does not change. Users keep their passwords because `auth.users` moves with the database. |
| Supabase PostgREST | `postgrest/postgrest` container on Azure Container Apps | Same REST API, same JWT, so `db_get`, `db_post` etc. and RLS work unchanged. |
| Vercel function (`api/index.py`, 60 s limit) | FastAPI container on Azure Container Apps | No 60 s limit. Built from a `Dockerfile` in the repo, image in Azure Container Registry. |
| Vercel Cron (06:00 UTC) | Container Apps Job on a schedule | Calls the same `/api/technofresh/cron` with `CRON_SECRET`. |
| Vercel env vars | Azure Key Vault, referenced by the container apps | `TECHNOFRESH_KEY`, Anthropic keys, robot login, JWT secret. |
| Vercel DNS for `zacodasboard.co.za` | Unchanged at first, then Azure DNS | See "Domain" below. |
| Vercel logs | Log Analytics workspace plus an availability alert on `/api/health` | |

The Supabase specific database bits that RLS relies on (the `auth` schema, `auth.uid()`, the `authenticated` and `anon` roles, the `on_auth_user_created` trigger) are recreated on Azure Postgres by the restore plus a small setup script. Supabase Storage and Realtime are not used today, so they are not moved.

**Domain `zacodasboard.co.za`:**

* Checked 15 October 2026: the domain's nameservers are Vercel's (`ns1/ns2.vercel-dns.com`), it points at Vercel's servers, and it has **no MX or TXT records** (no email on it), so moving it is simple.
* The .co.za registration itself stays where it is. Only where the name points changes.
* Steps: (1) add the custom domain to the Azure FastAPI container app and prove ownership with the TXT record Azure gives you, added in Vercel DNS; (2) Azure issues a free managed certificate; (3) at cutover, change the domain's record in Vercel DNS to point at Azure (keep the TTL low, 60 s, from the day before); (4) rollback is pointing it back; (5) after two quiet weeks, create an Azure DNS zone, copy the records, change the nameservers at the registrar to Azure's, then remove the project from Vercel.
* The desktop app (`desktop/config.json`) uses the domain, so it needs no change.

**Monthly cost, South Africa North** (Azure retail prices in rand, pay as you go, fetched from the Azure Retail Prices API on 15 October 2026, 730 hours a month):

| Item | Size | Rand a month |
|---|---|---|
| PostgreSQL Flexible Server compute | Burstable B1ms (1 vCore, 2 GiB), R 0,3537 an hour | about R 258 |
| PostgreSQL storage | 32 GB at R 2,48 a GB | about R 80 |
| PostgreSQL backups | free up to the provisioned storage size, then R 1,86 a GB | R 0 |
| Container Apps (API, Auth, PostgREST, always one replica each, 1 vCPU and 2 GiB in total) | consumption plan, mostly idle; monthly free allowance of 180 000 vCPU seconds and 360 000 GiB seconds | about R 400 to R 700 |
| Container Registry | Basic, R 2,74 a day | about R 83 |
| Log Analytics | first 5 GB a month free, then R 55 a GB | about R 0 to R 55 |
| Key Vault, DNS zone | | about R 15 |
| **Total** | | **about R 850 to R 1 200** |

If the database ever feels slow, Burstable B2s (2 vCores, 4 GiB) at R 1,41 an hour adds about R 775 a month. Today's data (about 1 000 sales rows, 356 payments) is tiny, so B1ms is enough for Zaco and the first few agencies. Prices exclude VAT, and the Container Apps figure is an estimate. Confirm in the Azure pricing calculator, and set a budget alert at R 1 500 on the resource group.

**Signing keys, checked 15 October 2026**: the project signs user tokens with an ES256 key published at its JWKS endpoint. The backend already verifies these through JWKS, so it needs no secret. Supabase does not hand out that private key, so self hosted Auth on Azure will sign with a new key. Every user (and the robot) signs in once more after cutover. Tell Zaco in advance.

**Steps:**

1. **Backup now** (done 15 October 2026: `Documents\Zacon-backups\zaco-full-2026-10-15_1355.dump`, row counts match live) with `scripts/backup_db.ps1` (full `pg_dump`, all schemas, kept outside the repo). Also download Supabase's own daily backup from the dashboard as a second copy.
2. **Containerise**: a `Dockerfile` for the FastAPI app (same code, `uvicorn` instead of `api/index.py`). Run it locally against the current Supabase to prove nothing changed. (Started on branch `infra/azure-container`: the image builds, passes its health check, serves the frontend, and refuses data without a login.)
3. **Provision** the Azure resources above (Bicep or a script with `az`, kept in `infra/` so it can be rebuilt). Approximate monthly cost to be confirmed with the Azure pricing calculator before creating anything.
4. **Rehearse**: restore the backup into Azure Postgres, run the auth setup script, point a test copy of the app at it (`test.zacodasboard.co.za` or the Azure default URL), sign in as an existing user, run the full test suite against it, and compare month by month totals and Tracking output with production (the method in `HANDOVER.md`). Fix and repeat until identical.
   **Practice run, 15 October 2026 — passed.** Scripts: `infra/setup.sh` (foundation), `infra/db/01_supabase_roles.sql`, `infra/db/02_after_restore.sql`, `infra/deploy-apps.sh` (four apps: `zacon-gw` public Caddy gateway, internal `zacon-api`, `zacon-auth` = `supabase/auth:v2.197.0`, `zacon-rest` = `postgrest/postgrest:v14.18`). Restore order that works: run `01`, run `auth migrate` once, restore `auth.users`, then `auth.identities`, then `public`, then `02`. Results: all tables match live row for row up to the backup time; a user signed in with their existing password; Tracking, Insights, Stock and Procurement load with the same figures as live; public sign up is off; an unsigned request sees no data. Practice address: `https://zacon-gw.happyground-50582879.southafricanorth.azurecontainerapps.io`.

   **To change before cutover:**
   * Create the database with Supabase's text sort order (ICU `en-US`) instead of Azure's default `en_US.utf8`: `create database zacon template template0 locale_provider icu icu_locale 'en-US' ...`, and point the apps at it. Data is identical either way; only the order of some text lists differs.
   * Users keep their passwords (checked). Existing sessions do not carry over, because the token signing secret is new, so everyone signs in once after cutover.
   * Add the remaining secrets to Key Vault (Anthropic keys, `TECHNOFRESH_KEY`, robot login, `CRON_SECRET`) and the 06:00 UTC scheduled job.
   * ~~Password reset emails need an SMTP sender.~~ Done 15 October 2026: Azure Communication Services (`email-zacon`, `acs-zacon`, data in Africa) sends from `DoNotReply@zacodasboard.co.za`; SPF, DKIM and ownership records added in Vercel DNS and verified. Resets are sent with `infra/send-reset.sh <email>`. When the DNS zone moves to Azure, copy these four records across.
   * ~~Sort order~~ Done: the apps run on database `zacon`, created by `infra/db/restore.sh` with ICU `en-US`. The old practice copy in database `postgres` can be dropped.
   * ~~Scheduled pull~~ Done: job `zacon-pull` (`infra/pull-job.sh`), manual until cutover, then `bash infra/pull-job.sh schedule`.
   * **Certificate rule (CAA)**: the domain only allows `pki.goog`, `sectigo.com` and `letsencrypt.org` to issue certificates. Azure Container Apps managed certificates come from DigiCert, so add `0 issue "digicert.com"` in Vercel DNS before adding the custom domain in Azure, or the certificate will not be issued.

5. **Cutover**, in an evening after the 08:00 pull and with Zaco told in advance: set the Supabase project to read only for the app (or simply stop the Vercel deployment), take a final `pg_dump`, restore it to Azure, run the checks again (row counts per table, totals per month), switch the domain record to Azure. Expected downtime: 15 to 30 minutes. The data is small (tens of thousands of rows).
6. **Next morning**: confirm the 08:00 Techno Fresh pull ran on Azure and saved.
7. **Keep Supabase and Vercel paused, not deleted, for 30 days** as the rollback. Rollback = point the domain back at Vercel and replay any rows written on Azure since cutover (export by `created_at`).
8. **Afterwards**: update `DEPLOY.md` and `HANDOVER.md`, then delete the Supabase project and Vercel deployment.

**Risks:**

* Auth differences: self hosted GoTrue must use the **same JWT secret or signing keys** as the old project, or every user is signed out and the robot cannot sign in. Mitigation: carry over the secret, or accept a one time sign out and tell users.
* Supabase extensions or functions not available on Azure Postgres (check `pg_extension` before the rehearsal; allow-list them in Azure's `azure.extensions` setting).
* The `.vercelignore` style protection (no `.env`, no client PDFs in the bundle) must be repeated in a `.dockerignore`.
* Running GoTrue and PostgREST yourself means you patch them. Pin versions, update monthly.
* Cost: Azure for this setup is likely somewhat more than Supabase Pro plus Vercel Pro. Confirm before provisioning.

**Not moving to South Africa**: the AI assistant (Anthropic) and, later, WhatsApp (Meta) process data abroad. Disclose these in the privacy notice. Sell it as "your data is stored in South Africa".

Effort: **3 to 5 weeks.** Agency #2 moves back by the same amount.

Estimates assume one experienced developer working with Claude Code, in focused weeks. Two people does not halve them. Ranges are honest: the low end is if nothing surprising turns up.

### Phase 1. Tenancy foundation, Zaco unchanged (minimum to onboard agency #2 safely)

**Goal**: a second agency can sign in and use Sales and Payments, Tracking, Insights and Stock with its own data, its own users and its own branding, and Zaco sees no change.

Data model:

* New: `agencies` (id, slug, legal name, trading name, APAC registration no., producer codes `text[]`, market codes `text[]`, timezone, branding json: colours, logo storage path, document footer; `competitor_status`; `plan`; `status` active/suspended), `agency_members`, `agency_modules`, `agency_settings` (key/value json for the business rules listed in 1.5, defaulting to today's values), `market_destinations` (agency_id, accsale prefix, market code, market name, agent name: replaces `DESTINATIONS`), `platform_rules`, `audit_log` (minimal version: who, agency, action, table, row id, before/after json, when).
* Add `agency_id` to all 19 existing tables except `profiles` (which gets it through `agency_members`).
* Rebuild unique keys with `agency_id` first: `product_codes (agency_id, product)`, `suppliers (agency_id, name)`, `statements (agency_id, market_agent, stm_no)` and the per sale day key from `0020`, `consignment_deals (agency_id, supplier_ref, product)`, `technofresh_days (agency_id, report, day)`, `technofresh_settings` primary key becomes `agency_id`.
* Replace every policy with the agency predicate. Keep the existing "admin may change roles" logic, now scoped per agency.
* `profiles.role` moves to `agency_members.role` with roles `owner`, `admin`, `office`, `salesman` (salesman used in Phase 7). Platform owner tracked in a separate `platform_admins` table.

Code to refactor:

* `supabase_auth.py`: `current_profile` returns `agency_id`, role, enabled modules, branding. Add `require_module` and `require_role` dependencies.
* Replace hardcoded 20026 with `agency.producer_codes` (seven modules, listed in 1.5). These functions are pure today, so pass the codes in as a parameter. The tests keep passing with `{"20026"}`.
* `payment_details.destination` and `market_stock.MARKETS` read from `market_destinations` and `agencies.market_codes`, passed in by the caller.
* PDFs (`order_sheet.py`, `dispatch_sheet.py`, `stocktake_sheet.py`) take a `brand` argument (name, logo, colours). File names use the agency slug.
* `assistant.py` prompts: build the "how the business works" paragraph from agency settings. Zaco's current wording becomes Zaco's stored text, not the code default.
* Frontend: read branding and modules from `/api/me`, apply colours to the existing `:root` tokens, swap the logo, hide tabs for disabled modules. Move the base64 logo to Supabase Storage (per agency folder, private bucket with RLS).
* Techno Fresh: minimum change in this phase is that settings and days are per agency and the cron loops over agencies that have it enabled. Full robustness is Phase 3. For agency #2 in Phase 1, manual PDF upload is acceptable.
* Procurement lock (section 3) ships in this phase, because it is a hard constraint. Procurement is enabled for Zaco only.
* Platform admin: a small owner only page or simply SQL scripts plus a CLI (`scripts/agency.py create|invite|module`) to create an agency and invite its first admin. A UI can wait.
* `/api/health`: remove anything agency specific (it is fine today).
* Domain: one app domain (e.g. `app.<product>.co.za`) with the agency picked from the login. Keep `zacodasboard.co.za` pointing at the same app with Zaco branding pre-applied on the login screen. Per agency subdomains can come later.

Migration of Zaco's live data, zero downtime (expand, backfill, switch, contract):

1. **Rehearse first.** Restore a copy of production into a Supabase branch or a scratch project (`pg_dump` / `pg_restore`), run the full migration there, run the test suite and the existing comparison method from `HANDOVER.md` (export the book, compare Tracking output before and after for every month). Only then touch production.
2. **Backup** immediately before: `pg_dump` of the whole database kept off Supabase, plus check that point in time recovery is on (Pro plan add on) or take a manual backup in the dashboard.
3. **Expand** (safe while the app runs): create `agencies`, insert Zaco with a fixed, known UUID. Insert `agency_members` for every existing profile, including the robot. Create `current_agency_id()`. Add `agency_id` as **nullable with default `current_agency_id()`**. Deployed code does not know about the column and still works; new inserts by Zaco users get Zaco's id automatically.
4. **Backfill**: `update ... set agency_id = '<zaco>' where agency_id is null`, table by table. Tables are small (the biggest is `statements` with tens of thousands of rows at most), so this is seconds. Run outside the 08:00 pull window.
5. **Switch** in one transaction per table: set `not null`, add the new unique constraint, drop the old one, drop old policies, create new ones. Because every existing user belongs to Zaco, `current_agency_id()` returns Zaco for all of them and they see exactly what they saw before. Unique index builds on tables this size are well under a second; use `create unique index concurrently` then `add constraint ... using index` on `statements` and `payments` to be safe.
6. **Deploy** the new backend and frontend (which now read agency, branding, modules). Zaco's branding is stored as data, so the screens look the same.
7. **Verify**: row counts per table before and after, Tracking totals per month before and after (the April check query in `HANDOVER.md` is a good template), the 08:00 pull the next morning, an RLS check as a fresh test agency user that returns zero Zaco rows.
8. **Contract** a week later: remove leftover code paths, the single row `technofresh_settings` check, and unused columns.

Rollback: steps 3 to 5 are reversible with a prepared down script (drop new policies, restore old ones, drop column). Keep that script tested in the rehearsal. If something goes wrong after step 6, revert the Vercel deploy first (instant), then decide on the database.

Risks:

* **A missed policy or a missed table leaks Zaco data.** Mitigation: generated policies, CI check, two agency test suite, and a manual review of every table and every `security definer` function before agency #2 gets a login.
* **Supabase `security definer` functions and views bypass RLS.** `is_admin`, `handle_new_user` and any views must be reviewed.
* **Storage**: PDFs kept in `technofresh_days.pdf` are tenant data and are covered by the table's RLS. Any future Storage bucket needs its own policies.
* **The AI assistant** sends book data to Claude. Make sure each call only ever loads the caller's agency (it goes through RLS, so it will). Chat threads are per agency.
* **Hidden Zaco rules** in the matching logic (e.g. Tshwane or Subtropico quirks in `tracking.py`) may misbehave for other markets. They are not a leak risk, but they are a correctness risk for agency #2. Treat agency #2 as a pilot.
* **Supabase Auth is shared**: one email can only exist once across all agencies. Fine for staff; matters for growers in Phase 4 (use phone numbers or per agency invite flows).

Effort: **4 to 6 weeks.** (Schema and policies 1 week, refactoring the hardcoded values and branding 1.5 to 2 weeks, tests and isolation suite 1 week, rehearsal and migration 0.5 to 1 week, platform admin scripts 0.5 week.)

### Phase 2. Agency onboarding and imports

Data model: `import_jobs` (agency, kind, file path, status, row counts, errors json, created_by), `import_mappings` (saved column mappings per agency per file layout), `growers` (see below), `products` (agency catalogue with commodity, variety, class, size, pack, and the short code currently in `product_codes`).

* Rename the concept `suppliers` to **growers** in the UI. In the database, either keep `suppliers` and add fields (grower number, ID or company reg, phone, WhatsApp opt in, bank details reference, POPIA consent) or create `growers` and migrate. Keeping the table and renaming later is the lower risk choice.
* Import flow: upload CSV or Excel, pick or save a column mapping, preview with validation (duplicates, unknown products, bad dates), then commit in one transaction. Store the raw file in a private per agency Storage folder. Every import can be rolled back by `import_job_id` (add that column to imported rows).
* History import: sales and payments history goes through the same parsers as today when the files are Techno Fresh layouts, and through the mapped CSV path otherwise.
* Onboarding checklist screen for the platform owner: agency created, admin invited, branding uploaded, markets and destinations set, competitor status classified, producer codes set, growers imported, products imported, history imported, Techno Fresh connected.

Refactor: `lookup.py` and `product_codes` fold into `products`. `csv_reports.py` becomes the "mapped import" engine.

Risks: messy spreadsheets (expect manual cleanup per customer; budget a day of your time per onboarding), duplicate growers, history that does not reconcile with what the market says. Never auto merge growers; show suspected duplicates.

Effort: **3 to 4 weeks.**

### Phase 3. Techno Fresh sync per agency, robust, with fallback

Data model:

* `sync_sources (agency_id, kind: technofresh|upload|other, username, secret_enc, enabled, schedule, last_ok_at, last_error)`.
* `sync_jobs (id, agency_id, report, day, status queued|running|done|failed|review, attempts, locked_until, message, file_path)` replacing the per agency use of `technofresh_days`. Keep `technofresh_days` readable for Zaco's history.
* Raw files move from base64 columns into Storage (`sync/<agency>/<report>/<day>.pdf`), referenced by path. Smaller rows, faster queries.

Design:

* **Scheduler**: Vercel Cron every 10 minutes during the morning window (say 06:00 to 10:00 SAST, then hourly until 18:00). Each tick claims a few due jobs (`update ... set status='running', locked_until=now()+2min where ... for update skip locked`) and works on them within the 60 second budget. Missed jobs simply get picked up next tick. This is the same proven pattern as today's `days_to_pull`, made per agency and resumable.
* **Who writes**: one robot user per agency (member of that agency only), so every write still goes through RLS. The scheduler needs a way to read "which agency is due" and that agency's robot credential without a user. Use the service role key **only** inside a small `scheduler.py` module for those two reads, never for book data. Robot passwords are random, stored encrypted.
* **Secrets**: one master key in the environment, per agency key derived with HKDF from the master key and the agency id, so a leaked row cannot be decrypted with another agency's context. Use `MultiFernet` so the master key can be rotated. (Supabase Vault is an alternative; the HKDF route keeps the current code shape.)
* **Robustness**: detect "logged out" and "page changed" separately; keep a small known good fingerprint of each report's header so a layout change fails loudly ("Techno Fresh changed the Daily Sales layout") instead of saving bad rows; retry with backoff; alert the agency admin and you by email (and later WhatsApp) after two failed mornings; a status page per agency showing last good pull per report.
* **Fallback**: the same screen accepts a manual upload of the PDF or CSV for any day, which goes through the same job record. For markets on another system (e.g. FreshMark), the mapped CSV/Excel import from Phase 2 is the fallback, with a saved mapping per agency.
* **Politeness**: keep the 2 second pace per session and stagger agencies. Check Techno Fresh's terms on automated access (Q8). Many agencies scraping one portal from one IP may get the IP blocked; plan for it.

Refactor: `technofresh.py` (credentials and robot become per agency arguments), `main.py:2440-2709` (pull logic moves into `sync.py`), `market_stock.MARKETS` loop uses the agency's market codes.

Risks: the portal changes or blocks you; credentials for many agencies in one place is a high value target (encrypt, restrict, audit every read); agents' reports may differ from producers' reports (Q1).

Effort: **3 to 4 weeks** for segment A (same Techno Fresh producer reports; each new market or agent only needs `market_destinations` rows). Agent side readers for segment B are a separate **6 to 10 weeks**, later.

### Phase 4. Grower portal

Data model: `grower_users (grower_id, auth_user_id, agency_id, invited_at, last_seen_at)`; RLS policies for growers that limit them to rows where `grower_id` is theirs **and** `agency_id` matches. This needs `grower_id` on the book tables (statements, payments or settlement lines), resolved through `consignment_deals` today. Add a direct `grower_id` column filled at import and sync time; it makes every grower query simple and RLS fast.

Design:

* Separate, simple pages (mobile first): my consignments, what sold at what price per day, what has been paid, statements to download. No access to other growers, market agents' names, or the agency's commission settings beyond what appears on their statement.
* Sign in by phone number with a one time code (Supabase phone auth with an SMS provider, or a WhatsApp OTP once Phase 5 is live), or a magic link by email. Growers will not manage passwords.
* A grower who consigns to two agencies on the platform gets two separate logins or an agency picker. Never a merged view across agencies.
* Policy design: grower policies are separate from staff policies and are tested by the same isolation suite (grower A vs grower B within one agency, and across agencies).

Refactor: Tracking and statements code currently computes at agency level; it needs a "for one grower" filter. Settlement logic from Phase 6 should land before or with this phase, because a portal showing unsettled figures without statements will cause support calls.

Risks: growers seeing figures that disagree with what the agency told them (only publish settled statements, or clearly mark "provisional"); data exposure through mistakes in grower policies; support load.

Effort: **3 to 4 weeks** (after Phase 6).

### Phase 5. WhatsApp notifications

Data model: `notification_templates` (per agency, per event, language), `notifications` (outbox: agency, grower, channel, template, payload, status, provider id, sent_at, error), `growers.whatsapp_opt_in_at` and `opt_out_at`.

Design:

* WhatsApp Business Platform (Cloud API from Meta directly, or a BSP such as Twilio or 360dialog). Each agency needs its own approved sender number and business verification, or the platform sends from one number "on behalf of" agencies. One number is simpler to run; per agency numbers look more professional. Q9.
* Events: "your produce sold" (daily summary per grower, not per sale), "payment sent" (on settlement). Templates must be pre approved by Meta and are billed per conversation.
* Outbox pattern: events write a row; the cron sends due rows; retries with backoff; status webhooks update the row. Nothing sends from inside a request.
* POPIA: send only to growers who opted in, store the consent, honour STOP.

Risks: Meta approval delays (weeks), per message costs (pass through as an add on), sending wrong figures to growers (send only from settled data, add a daily "hold" switch per agency).

Effort: **2 to 3 weeks**, plus waiting time for Meta approval.

### Phase 6. Grower statements, commission and payment reconciliation

Data model:

* `commission_rules` (agency, scope: default, per grower, per product, per deal; percentage; fixed fees per carton; effective from/to). Today's `suppliers.default_commission_pct` and `consignment_deals.commission_pct` become rows here.
* `deductions` (market levy, agent commission, transport, packaging, cold storage, advances) per consignment or per grower.
* `grower_statements` (agency, grower, period, status draft|issued|paid, totals, pdf path, issued_at) and `grower_statement_lines`.
* `grower_payments` (amount, date, method, bank reference, statement id, reconciled bank line).
* `bank_lines` (imported bank statement lines for reconciliation, from CSV/OFX).

Design: statement run per period per grower, preview, issue (locks figures), PDF, mark paid, match to the bank import. All amounts in cents as integers or `numeric(14,2)`; never floats for money (today the code uses Python floats in `tracking.py`; that is acceptable for display but not for issued statements).

Refactor: `consignment_deals` settlement fields, `tracking.py` settlement summaries, `reconcile.py`. Agency vs producer model (Q1) changes what "commission" means: for a market agent it is the agent's commission on gross sales; for Zaco it is a share of what the market agent returned. Model both as rules applied to a chosen base (`gross` or `nett`).

Risks: getting money wrong is the most damaging bug this product can have. Issue statements only from locked data, keep every issued statement immutable (corrections are new adjusting statements), and test against Zaco's real settlements before any other agency uses it.

Effort: **4 to 6 weeks.**

### Phase 7. Compliance reports (APAC, trust accounts, unclaimed monies)

For segment A, only the grower balances and unpaid or unclaimed grower monies reports are needed early. The trust account and APAC audit reports are for segment B (registered market agents) and move to that later stage.

Data model: mostly views over Phase 6 tables. Add `trust_account` (agency's trust bank account details), `unclaimed_monies` (grower, amount, since, attempts to contact, status) or a view that flags grower balances unpaid past N days.

Reports:

* Trust account reconciliation: money received from buyers or markets vs money paid to growers vs commission transferred to the business account, per period, with differences listed.
* Grower balances aged (0 to 30, 31 to 60, 61 to 90, 90+ days).
* Unclaimed and unpaid grower monies with contact attempts.
* Audit pack: a single PDF/Excel bundle per period for the auditor.

I do not know the exact APAC rules (Agricultural Produce Agents Act and its regulations, trust account audit requirements, rules on unclaimed money). These reports must be specified with an APAC registered agency and ideally their auditor (Q6). Build only after Phase 6 is trusted.

Effort: **2 to 3 weeks** once the rules are known.

### Phase 8. Dashboards

* Price per product per day (min, average, max, volume) from `statements`, with market average where the reports carry it (`market_avg` column exists since `0013`).
* Stock on the floor and ageing (from `market_stock` and `stock_notes`; days since delivery, coloured bands).
* Top growers and top buyers by value and volume (buyers depend on whether the agency's reports carry buyer names; producer reports do not).

Much of this exists in Insights and Stock (`analytics.py`, `stock_book.py`, `market_stock.py`). Mostly refactor to run per agency and add filters. Add Postgres views or materialised daily rollups if queries slow down; today's Python side aggregation over all rows will not scale to many agencies with years of data.

Effort: **2 to 3 weeks.**

### Phase 9. Mobile views for salesmen

* `salesman` role with a narrow, fast screen: today's stock on the floor per product, today's prices, what sold so far today, notes. Large tap targets, works on a cheap Android phone on poor signal.
* The current single file frontend already works on phones. Add a separate lightweight page (`/floor`) rather than more branches in `index.html`. Make it a PWA (installable, caches the last view offline).
* Role gating in the backend: salesmen cannot see grower payments, commissions or settings.

Effort: **2 weeks.**

### Phase 10. Audit logs, backups, POPIA basics

* **Audit log**: the minimal table from Phase 1 grows into triggers on all financial tables (insert, update, delete with before and after), plus app level events (sign in, export, settings change, module toggled, support access by you). Read only for agency owners, filterable.
* **Backups**: enable Supabase point in time recovery. Add a nightly `pg_dump` to separate storage you control (e.g. an S3 bucket in another provider), with a monthly restore test. Per agency export (all their data as CSV/Excel in a zip) so you can honour offboarding and POPIA access requests.
* **POPIA**: register an Information Officer for your company; a privacy notice and operator agreement (you are the operator, each agency is the responsible party); consent capture for growers (WhatsApp, portal); data retention settings per agency (e.g. keep financial records 5+ years as tax law requires, delete chat threads after N months, delete raw report PDFs after N months); a documented breach procedure; access control review per role. Data is hosted outside South Africa if the Supabase region is not in SA; disclose cross border transfer (Q10).
* **Support access**: you should not see agency data by default. "Act as agency" for support is time limited, logged and visible to the agency owner.

Effort: **2 to 3 weeks** (audit triggers and backup automation), plus legal documents outside the code.

### Phase 11. Subscription plans, limits and feature flags

Data model:

* `plans (code: starter|growth|pro, name, limits json, included_modules text[])`. Example limits: users, growers, markets, sync sources, history months, grower portal on/off, WhatsApp messages per month.
* `addons (code: procurement|whatsapp|grower_portal|dedicated_db, price)` and `agency_addons (agency_id, addon, active_from, active_to)`.
* `agency_modules` from Phase 1 stays the **single source of truth for what is switched on**. A function `effective_modules(agency)` = plan's included modules + active add ons + manual overrides by you, minus anything blocked (procurement for competitors). The result is written into `agency_modules`, so the rest of the app only ever checks one table.
* Limits are checked where things are created (inviting a user, creating a grower, adding a sync source) with a clear message. Never block reading or exporting existing data because of a limit.
* Billing integration later (Paystack or PayFast for South African cards and debit orders, or Stripe). Until then you invoice manually and set the plan by hand.

Rule to keep: **plan and add ons can grant modules; only the competitor rule can deny procurement, and it always wins.**

Effort: **1.5 to 2 weeks** (without billing integration).

---

## 5. Order of work

| Order | Phase | Why here | Effort |
|---|---|---|---|
| 0 | Answer Q2 to Q5 with Zaco; get sample reports from the introduced agency | Q1 is answered (segment A first). The competitor rule must be agreed before agency #2 is classified | 1 week of conversations |
| 0b | **Phase 0** Move to Azure South Africa, like for like, domain included | Data in South Africa before any second agency's data exists; one customer to move, not many | 3 to 5 weeks |
| 1 | **Phase 1** Tenancy foundation, procurement lock, branding, minimal audit | Nothing can be sold safely without it. Zaco unchanged. | 4 to 6 weeks |
| 2 | Part of Phase 10: backups, PITR, restore test, POPIA paperwork | Before a second customer's data exists | 1 week |
| 3 | Phase 2 Onboarding and imports | Needed for agency #2's growers, products, history | 3 to 4 weeks |
| 4 | Phase 3 Sync per agency | Agency #2 can start on manual uploads, but daily use needs this | 3 to 4 weeks (+ parsers per Q1) |
| | **Pilot: onboard agency #2** (the introduced segment A business, classified before signing) | Learn before building more | 2 to 4 weeks alongside |
| 5 | Phase 11 Plans and flags (no billing) | Before agency #3, so pricing is consistent | 1.5 to 2 weeks |
| 6 | Phase 6 Statements, commission, reconciliation | Core value for market agencies, prerequisite for portal and WhatsApp | 4 to 6 weeks |
| 7 | Phase 8 Dashboards and Phase 9 mobile salesmen views | Mostly reuse; good selling points | 4 to 5 weeks |
| 8 | Phase 4 Grower portal | Needs settled statements | 3 to 4 weeks |
| 9 | Phase 5 WhatsApp | Needs statements and opt in data; start Meta approval early (during step 6) | 2 to 3 weeks |
| 10 | Phase 7 Compliance reports | Needs trusted statements and the APAC rules | 2 to 3 weeks |
| 11 | Rest of Phase 10: full audit triggers, retention jobs, support access | Before selling to larger agencies | 2 weeks |
| Later | **Segment B: market agents and the supplier side.** Agent side report readers (6 to 10 weeks), trust account and APAC audit reports, grower or packhouse own views. Needs Zaco's written agreement first | After segment A is live and paying | 10 to 16 weeks |
| Later | Billing integration, per agency subdomains, dedicated DB add on, flower agents | | |

Rough total to a sellable product with grower portal and compliance: **35 to 50 developer weeks**. To agency #2 live on the basics: **11 to 15 weeks**.

---

## 6. Keeping the door open for flower, export and distributor customers

Do not build for them now. Avoid these lock ins:

* Do not hardcode "fruit", "carton", "market" in new tables. Use `unit` (carton, bunch, box, pallet, kg) and `channel` (market agent, export buyer, retailer) fields with today's values as defaults.
* Keep the parser and sync layer pluggable: one interface (`fetch(day) -> rows`, `parse(file) -> rows`) with Techno Fresh as the first implementation.
* Commission rules apply to a chosen base (gross, nett, or a fixed price), not only "share of nett".
* Currency stored per agency (ZAR default). Export agents deal in USD and EUR.
* Product catalogue with free form attributes (json) beside the fruit specific ones.

---

## 7. Practical principles for a small team

* One code base, one deployment, one database for all agencies. No per customer forks; customer differences live in data (settings, mappings, branding, modules).
* Every new table gets `agency_id`, RLS and an isolation test in the same change. Make this a checklist item and a CI check.
* Ship Phase 1 in small, reversible steps; Zaco is the canary for every deploy.
* Keep the "verify against Zaco's live book, old code vs new code" method from `HANDOVER.md` for every change to matching or money logic.
* Split `main.py` into routers (`sales`, `tracking`, `procurement`, `stock`, `assistant`, `sync`, `admin`) during Phase 1. It makes module gating per router one line, and it is low risk because routes are already grouped.

---

## 8. Open questions for you

**Business model and Zaco**

1. ~~Is Zaco a market agent or a consignment intermediary?~~ **Answered 15 October 2026:** a consignment intermediary, as the code says. Segment A (businesses like Zaco) first, market agents and the supplier side later. Still needed: one sample of each Techno Fresh report (Daily Sales, Payment Details, Stock On Hand) from the introduced agency, to confirm the readers work unchanged.
2. **Definition of "direct competitor of Zaco"**: same market? Same markets *and* same commodities? Anyone delivering to the same market agents? Are the market agents Zaco uses (Farmers Trust, Subtropico etc.) themselves off limits as customers, for procurement or for the whole product?
3. Is Zaco happy for **non competitors** to get procurement at all, built on logic developed for Zaco? Should Zaco's tuned numbers (weights, thresholds, prompts) stay Zaco only?
4. Does Zaco's agreement require **physical separation** (its own database) or is logical separation with RLS acceptable? Does Zaco need to be told, or give consent, before other agencies go on the same platform?
5. Who owns Zaco's data contractually, and what happens to it if Zaco leaves? Is there a written agreement with Zaco covering the procurement restriction?

**Compliance**

6. Which APAC rules must the compliance reports cover (trust account audit format, unclaimed monies rules, retention periods)? Can you introduce us to an agency's auditor for a spec?
7. Do you (your company) have a POPIA Information Officer and an operator agreement template? Will you need customers' data hosted in South Africa?

**Techno Fresh and other systems**

8. Do Techno Fresh's terms allow automated access with a customer's credentials? Is there any chance of an official export or API if approached as a vendor? Which markets your targets use run something other than Techno Fresh?
9. WhatsApp: one platform sender number for all agencies, or each agency its own? Who pays per message costs?

**Product and operations**

10. Which Supabase region is the project in, and is the current plan Pro (point in time recovery, branching)?
11. Is public sign up disabled in Supabase Auth today? (If not, it must be before anything else.)
12. Product name and domain for the multi agency version (not "ZacoAgents", not `zacodasboard.co.za`)? Does Zaco keep its own domain?
13. Who is the introduced agency #2, roughly how big (users, growers, consignments per month), which markets and market agents do they deliver to, and are they a competitor of Zaco under the agreed rule?
14. Rough price points for Starter, Growth and Pro, and which limits matter most to customers (users, growers, markets, history)?
15. Will the Electron desktop app be offered to other agencies, or is the web app (installable as a PWA) enough?
16. Do growers have smartphones and data, and do they prefer Afrikaans, English, or other languages for statements and messages?
17. Should the AI assistant be included for other agencies, and in which plan? It sends their book data to Anthropic, which needs disclosing under POPIA.
