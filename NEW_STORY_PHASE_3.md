# New Story Phase 3 — Legacy Data Migration + Operations Data

This build turns the uploaded `New Story Device Tracker (1).xlsx` into a one-time PostgreSQL migration snapshot. The live application does not depend on the workbook after import.

## Added operational data areas
- Procurement (`Incoming Items`)
- Service History (`Returned Devices`)
- Installations (`On-site Install`)
- Domains & enrollment reference (`NSS DOMAINS`)
- One-time Legacy Import screen

## Legacy migration snapshot
The bundled seed contains:
- 2,824 normalized customer requests
- 5,583 request-linked assets
- 4,290 shipment/return records
- 414 additional inventory assets not duplicated from the request ledgers
- 327 procurement/order lines
- 1,360 returned-device/service events
- 112 installation records
- 14 domain reference rows
- 41 CY operational notes (linked to matching tickets where possible)

Device-tab rows are grouped into ticket/request records rather than reproduced as spreadsheet tabs. Serial normalization preserves the legacy Chromebook/Windows rules, while raw serials are also retained.

## How to import
After deployment, sign into the Pierson admin side and open:

`/new-story/legacy-import`

Review the counts and click **Import legacy data now**. The workbook hash is recorded so the same snapshot cannot be imported twice.


## Import hotfix
- Changed `new_story_activity.summary` from `VARCHAR(300)` to `TEXT` so long legacy operational notes do not abort the PostgreSQL import.
- PostgreSQL startup migration now widens the existing column automatically.
