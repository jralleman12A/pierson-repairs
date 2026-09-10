# Pierson Repairs

Boxlight panel repair tracking with a customer-facing client portal.

## Two surfaces

| Surface | URL | Who | Auth |
|---|---|---|---|
| Staff admin | `/login` → `/dashboard` | Pierson staff | `User` table, hashed |
| Client portal | `/` or `/portal/login` → `/portal` | Customers (MCPS) | `ClientAccount` table, hashed |
| Boxlight portal | same public login → `/boxlight` | Boxlight reps | `BoxlightAccount` table, hashed |

Sessions are namespaced (`admin_user_id`, `client_portal_id`, and `boxlight_account_id`) so the
three surfaces do not overwrite one another during normal use.

The driver portal and the old shared-password `/customer` view have been removed.
Old URLs redirect rather than 404.

## Data scoping

Every `Unit` has a `client_id`. The client portal only ever queries units matching
the signed-in client. Units with no `client_id` are **invisible in every portal** —
assign them from the admin dashboard.

Repair notes default to internal. Tick "Show this note in the client portal" on a
note to surface it to the customer.

## Environment variables

Required:
- `SECRET_KEY` — Render generates this
- `DATABASE_URL` — from the linked Postgres instance
- `UPLOAD_FOLDER` — must point at the persistent disk (`/var/data/uploads`)
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` — creates the first staff login on boot

First-run client (optional, can be removed after the account exists):
- `BOOTSTRAP_CLIENT_COMPANY`, `BOOTSTRAP_CLIENT_USERNAME`, `BOOTSTRAP_CLIENT_PASSWORD`

First-run Boxlight rep (optional):
- `BOXLIGHT_USERNAME`, `BOXLIGHT_PASSWORD`

Email reports (optional):
- `GMAIL_USER`, `GMAIL_APP_PASSWORD`

Tuning (all have defaults):
- `SESSION_TIMEOUT_MINUTES` (120), `MAX_UPLOAD_MB` (25),
  `LOGIN_MAX_ATTEMPTS` (8), `LOGIN_WINDOW_SECONDS` (900)

## Deploying

`init_database()` runs on boot: `create_all()`, then adds `units.client_id` and
`repair_notes.is_internal` if missing. If exactly one `ClientAccount` exists, all
unassigned units are attached to it — so an existing MCPS-only database migrates
with no manual work.

**Back up the database before the first deploy of this version.**

## Local

```
pip install -r requirements.txt
set FLASK_DEBUG=1
python app.py
```

Falls back to SQLite (`repair_tracker_local.db`) when `DATABASE_URL` is unset.
