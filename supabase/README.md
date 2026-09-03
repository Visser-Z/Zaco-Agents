# Supabase setup

Postgres, authentication and row level security for Zacon.

## 1. Create the project

1. Sign up at <https://supabase.com>, create a project.
2. Choose the region closest to the users — **Johannesburg / South Africa**
   if offered, otherwise the nearest to it. The VPS is in `jnb1`, and every
   query the backend makes crosses this link.
3. Save the database password somewhere safe; it is shown once.

## 2. Run the migration

Open **SQL Editor → New query**, paste the whole of
`migrations/0001_init.sql`, and run it. It creates:

| Object | Purpose |
|---|---|
| `user_role` enum | `admin` \| `staff` |
| `profiles` | one row per user, carries their role |
| `product_codes` | PDF product string → short code (replaces the JSON file) |
| `statements` | durable record of every statement processed |
| RLS policies | who may read/write each table |
| `handle_new_user` trigger | creates a profile on signup, defaulting to `staff` |
| `prevent_role_escalation` trigger | stops a user promoting themselves |

## 3. Turn off public signup

**Authentication → Providers → Email**, disable **"Enable sign ups"**.

From then on the only way in is an invitation: **Authentication → Users →
Invite user**. Anyone who reaches the login page without an invite can do
nothing with it.

## 4. Make yourself an admin

The first user to sign up is created as `staff` like everyone else — the
trigger has no way to know who the owner is. Promote yourself once, from the
SQL editor:

```sql
update public.profiles set role = 'admin' where email = 'you@example.com';
```

After that, admins can manage roles through the app.

## 5. Collect the keys

**Project Settings → API**:

| Key | Used by | Safe to expose? |
|---|---|---|
| Project URL | frontend + backend | yes |
| `anon` public key | frontend | yes — RLS is what protects the data |
| `service_role` key | backend, admin tasks only | **no — never send to a browser** |

## How RLS is meant to work here

**Authentication answers "who are you". RLS answers "what may you touch".**
They are separate, and only the second one protects a row.

The backend must call Supabase with **the caller's JWT**, forwarded from the
frontend — not with `service_role`. `service_role` bypasses RLS completely, so
using it for ordinary requests would silently disable every policy in the
migration while appearing to work perfectly.

Reserve `service_role` for operations that are genuinely administrative and
have no user context, such as inviting a new user.

## Role summary

| Action | staff | admin |
|---|---|---|
| Read statements, product codes, profiles | ✅ | ✅ |
| Record a statement, add a product code | ✅ | ✅ |
| Edit or delete statements / product codes | ❌ | ✅ |
| Change another user's role | ❌ | ✅ |
| Change **own** role | ❌ | ✅ |

Staff cannot delete financial records; corrections go through an admin. That is
deliberate — an append-only history for ordinary users is what makes the
`statements` table trustworthy as an audit trail.
