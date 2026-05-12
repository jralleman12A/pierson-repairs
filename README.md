# Pierson Repairs — Render Production App

This is the unified hosted version of the Boxlight Tracker.

It replaces the split setup:

```text
MDT internal tracker + JSON sync + external customer portal
```

with one hosted app:

```text
Render Flask app + PostgreSQL + persistent upload disk
```

## What is included

- Admin login
- Internal repair dashboard
- Unit add/edit/archive
- Repair notes
- Status updates
- Repaired Date / Delivery Date
- Tech check-off slip upload/view
- Customer portal
- Customer check-off slip viewing
- Packing slips
- CSV export
- PostgreSQL support
- Render deployment files
- SQLite import script

## Important Render settings

Use these commands if you configure manually instead of using `render.yaml`:

```text
Build Command:
pip install -r requirements.txt

Start Command:
gunicorn app:app
```

Set environment variables:

```text
SECRET_KEY = generate a long random string
DATABASE_URL = your Render PostgreSQL internal connection string
UPLOAD_FOLDER = /var/data/uploads
CUSTOMER_PORTAL_PASSWORD = your customer password
ADMIN_USERNAME = admin
ADMIN_PASSWORD = your temporary first admin password
```

Add a persistent disk on Render:

```text
Mount path: /var/data
Size: 5 GB or larger
```

## First login

If `ADMIN_USERNAME` and `ADMIN_PASSWORD` are set, the app creates the admin user automatically the first time it starts.

Default if you do nothing locally:

```text
Username: admin
Password: ChangeMe123!
```

Change this in Render immediately.

## Import your existing SQLite database

After the hosted app is deployed and connected to PostgreSQL, run:

```bash
python scripts/import_sqlite_to_postgres.py /path/to/repair_tracker.db
```

For local testing, put your existing SQLite database somewhere accessible and run the importer with `DATABASE_URL` pointing at the target database.

## Upload/check-off files

Uploaded slips are stored in:

```text
/var/data/uploads/checkoff_slips
```

on Render when the persistent disk is mounted.

Do not use GitHub for uploaded check-off slips anymore.

## Cutover plan

1. Deploy this app to Render.
2. Create/attach PostgreSQL.
3. Attach persistent disk.
4. Set environment variables.
5. Import SQLite data.
6. Upload or migrate existing check-off slip files if needed.
7. Test admin login.
8. Test customer portal.
9. Point `repairs.pierson.it` to Render.
10. Stop using the MDT-hosted tracker.
