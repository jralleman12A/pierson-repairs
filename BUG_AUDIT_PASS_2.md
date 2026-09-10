# Pierson Repairs — Second Audit Pass

Audit date: 2026-09-10

## Scope

This was a second independent pass over the already-audited repository, aimed at edge cases the first structural audit could miss.

Checked again:

- Python syntax / AST parse for application code
- Jinja syntax for every template
- All Flask endpoint names referenced by `url_for()`
- `render_template()` targets
- Referenced static files
- POST forms, POST-capable endpoints, and CSRF tokens
- Authentication decorators on admin, client, and Boxlight routes
- Client record/file lookup scoping
- Boxlight repair, stock, export, and messaging route wiring
- Null/legacy-data behavior in templates
- Startup database migration/backfill behavior
- Replacement-stock assignment input handling
- Render health-check behavior
- Persistent upload configuration

## Automated/static results after fixes

- Python compile: PASS (`app.py`, `fix_brands.py`)
- Flask route declarations discovered: 79 across 70 endpoint functions
- Jinja parse errors: 0
- Missing `url_for()` endpoints: 0
- Missing `render_template()` targets: 0
- Missing referenced static assets: 0
- POST forms missing CSRF token: 0
- POST forms targeting a non-POST route: 0
- Remaining obvious nullable `truncate` / unguarded `length` patterns: 0

## Bugs fixed in this second pass

### 1. Unassigned repairs could be silently assigned to the only client on app restart

`backfill_unit_clients()` ran on every startup. If the system had exactly one client account, every unit with `client_id = NULL` was automatically assigned to that client.

That contradicted the admin UI rule that unassigned units are not visible in any client portal and could expose a repair that was intentionally left unassigned.

Fixed: legacy backfill is now opt-in only with `BACKFILL_UNASSIGNED_UNITS=true`. Normal boots preserve unassigned records exactly as they are. If the migration switch is used, the action is logged.

### 2. Lightweight migration failures were silent

`run_migrations()` rolled back on an ALTER failure but did not log the exception. A production schema mismatch could therefore survive startup and only show itself later as an unrelated 500.

Fixed: migration failures now emit a Render log traceback including the failing statement.

### 3. Render health check did not check PostgreSQL

`/health` returned 200 even if PostgreSQL was unreachable. Render could consider the web service healthy while every DB-backed page was broken.

Fixed: `/health` now performs `SELECT 1`. It returns 200 with `database: ok` when successful and 503 when the database is unavailable.

### 4. Boxlight dashboard had another nullable Jinja edge case

The repairs page had previously been fixed for nullable `final_outcome`, but the Boxlight overview still passed `reported_issue` directly through Jinja's `truncate` filter. Legacy/migrated NULL values could crash the overview.

Fixed: the value is normalized to an empty string before truncation.

### 5. Admin client EOD preview had an unguarded NULL length check

The preview displayed a safe fallback for `work_completed`, but its separate length test did not. A legacy NULL value could trigger a template error.

Fixed: the length check now uses the same null-safe fallback.

### 6. Replacement-stock assignment could 500 on malformed input

The assignment route called `int(unit_id)` without handling invalid input. The normal dropdown submits integers, but stale/tampered requests could cause an uncaught ValueError and a 500.

Fixed: invalid or missing repair selections now return a normal admin warning instead of crashing the request.

## Security / isolation checks

- Client repair detail lookups are scoped by both `client_id` and `unit_id`.
- Client check-off access is scoped to the signed-in client's unit.
- Client file downloads are scoped to the signed-in client's file records.
- Boxlight and client sessions use separate session keys.
- Admin routes examined remain behind `admin_login_required`.
- Boxlight data/actions examined remain behind `boxlight_login_required`.
- Uploaded filenames are normalized with `secure_filename()` before storage.
- Render uses a persistent disk for uploaded client/check-off files.

## Known design decisions / remaining non-breaking risks

These were not changed in this pass because they require business decisions rather than a bug fix:

1. **One original repair can still have multiple replacement panels assigned.** If one failed unit should consume exactly one warranty replacement, enforce that rule before relying on stock reports as an accounting control.
2. **Boxlight sees all active repair records.** That is fine while this application is effectively the Boxlight service program. If Pierson adds unrelated manufacturers/programs, Boxlight access needs explicit program/manufacturer scoping first.
3. **All Boxlight accounts share the same company message board.** There is no per-rep thread ownership; this currently behaves as a company-wide Boxlight inbox.
4. **Login throttling is in-process memory.** It is adequate for the current single-instance setup, but it is not a distributed rate limiter if the service later scales to multiple workers/instances.
5. **Dates remain string columns.** New entries use ISO dates, but older migrated records can contain other formats. Turnaround calculations already guard known formats, while future reporting would benefit from real SQL date columns.
6. **Automated weekly email delivery is not implemented yet.** The existing report sender can send manually; scheduled delivery still needs a Render Cron Job or equivalent.

## Live checks still required after deploying this build

Static analysis cannot prove infrastructure state. Before building the next feature, run this short production smoke test:

1. Open `/health` and verify it reports `database: ok`.
2. Admin login and open the main repair dashboard.
3. Open/edit one repair, update status/dates, and return to its detail page.
4. Open a client portal and verify only that client's repairs/files appear.
5. Open Boxlight Overview -> View all repairs -> repair detail -> Back to repairs.
6. Send a Boxlight message, reply as admin, and confirm unread state clears on view.
7. Add a test replacement serial and assign/unassign it.
8. Run admin, Boxlight-repair, and Boxlight-stock CSV exports.
9. View one existing check-off slip and download one client file.

If all nine pass, this codebase is in a good place to start the shipment/exchange notification workflow.
