# Pierson Repairs — Repo Health Check

Audit date: 2026-09-09

## What was checked

- Python syntax for `app.py` and `fix_brands.py`
- All Flask route endpoint names extracted from `app.py`
- Every Jinja `url_for()` reference compared against actual Flask endpoints
- Required route parameters in template `url_for()` calls
- Every `render_template()` target checked for a matching template file
- Jinja syntax parsed for every HTML template
- `{% extends %}` / `{% include %}` targets checked
- Static assets referenced with `url_for('static', ...)` checked for existence
- POST forms checked against POST-capable Flask routes
- POST forms checked for CSRF tokens
- Direct hard-coded internal links reviewed
- Render blueprint, upload storage configuration, and bootstrap environment variables reviewed
- Replacement stock, client portal, Boxlight portal, messaging, and export route wiring reviewed

## Results

### Passed

- `app.py` compiles successfully.
- `fix_brands.py` compiles successfully.
- No Jinja syntax errors were found in the templates.
- No template calls a missing Flask endpoint.
- No template `url_for()` call is missing a required route parameter.
- No `render_template()` call points to a missing template.
- No missing referenced Pierson logo/static asset was found.
- All 30 detected POST forms include a CSRF token.
- All detected POST form actions point to routes that accept POST.
- Render is configured with a persistent disk at `/var/data` and `UPLOAD_FOLDER=/var/data/uploads`, so uploaded check-off/client files are not being written to the ephemeral app filesystem.

## Bugs fixed in this audit

### 1. Client dashboard repair-rate math was inaccurate

`client_repair_stats()` previously calculated repaired panels as `total - scrapped`. That counted panels still Awaiting Diagnosis, Picking Up, In Repair, or Waiting on Parts as repaired. The displayed "repaired rather than replaced" number could therefore be materially wrong.

Fixed: only Completed and Returned-stage units now count as repaired. The repair rate is calculated only from units with a known final disposition (repaired vs scrapped).

### 2. Editing a unit could falsely report a duplicate Intake ID

The edit route caught every database exception and displayed "the Intake ID may already exist," even when the failure had nothing to do with the Intake ID.

Fixed: `IntegrityError` is handled as an actual duplicate/constraint error. Unexpected database errors are logged to Render and shown as a generic database error instead of a false duplicate message.

### 3. Admin "View as Client" link was misleading/broken

The Client Admin detail page linked directly to `/portal/dashboard`. An admin session is not a client session, so this normally redirected to the customer login rather than actually showing the selected client's view.

Fixed: the control is now labeled **Open Client Login** and explicitly opens the unified client login with the dashboard as the return destination. No insecure client impersonation was added.

### 4. Render blueprint did not declare Boxlight bootstrap credentials

The application supports `BOXLIGHT_USERNAME` and `BOXLIGHT_PASSWORD`, but the supplied `render.yaml` did not declare them.

Fixed: both are now present as secret/manual environment variables in `render.yaml`.

### 5. Local launch script did not create a bootstrap admin reliably

`run_local.bat` set `ADMIN_PASSWORD` but not `ADMIN_USERNAME`, while the application only bootstraps an admin when both are set.

Fixed: local script now sets a local admin username, local-only secret key, disables secure cookies locally, and enables Flask debug mode.

## Items that are wired correctly but need real deployment testing

The following require Flask/Postgres/Render runtime state and cannot be proven solely by static source inspection:

- PostgreSQL migrations against the live existing schema
- Gmail SMTP authentication and actual mail delivery
- Persistent-disk read/write permissions on the deployed Render instance
- Existing uploaded check-off/client files matching database metadata
- Real account/password state in the Render database
- Browser behavior across admin/client/Boxlight sessions

## Design / operational observations (not code-breaking bugs)

- `/boxlight/repairs` and its detail links are currently wired to real endpoints correctly.
- Boxlight messaging routes and both admin/Boxlight reply forms are wired correctly and CSRF-protected.
- Replacement-stock assignment is functional, but the current database design allows multiple replacement panels to be assigned to the same original repair. If the business rule should be exactly one replacement per failed panel, add a uniqueness rule or validation.
- The email report frequency setting is stored correctly, but automatic weekly/biweekly/monthly delivery is not implemented by the web process itself. The UI correctly notes that a Render Cron Job is required.
- The Boxlight portal currently sees all active repair units. If the tracker later contains non-Boxlight repair programs, add a manufacturer/service-program scope before giving external reps access.

## Suggested live smoke test after deploy

1. Staff login -> dashboard -> open/edit a unit -> status update -> dates -> check-off slip.
2. Staff -> Clients -> open a client -> Open Client Login -> sign in as that client.
3. Client -> Dashboard -> Repairs -> repair detail -> check-off/packing slip if present.
4. Boxlight -> Overview -> View all repairs -> repair detail -> Back to repairs.
5. Boxlight -> Messages -> create thread -> Admin Boxlight Messages -> reply -> Boxlight sees unread reply.
6. Admin -> Replacement Stock -> add a replacement -> assign it -> confirm Boxlight stock and repair detail show the linkage.
7. Run both Boxlight CSV exports and the admin CSV export.
8. Upload one test client file and one test check-off slip, then download/view both.
