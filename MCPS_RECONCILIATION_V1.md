# MCPS / Boxlight Reconciliation V1

This build preserves the existing repair workflow and adds a separate reconciliation layer.

## Added
- Admin **Reconciliation** page.
- Upload `.eml`, Outlook `.msg`, `.txt`, `.html`, or ZIP bundles from the website.
- Stores the original source file plus parsed email text and metadata.
- Extracts likely Boxlight panel serials, MCPS asset IDs, and RMA/case/ticket IDs.
- Compares extracted serials against existing `Unit` repairs and `ReplacementPanel` warranty stock.
- Classifies evidence as Matched, Possible Match, Needs Review, Conflicting Data, Missing from Pierson, Resolved, or Ignored.
- Manual review can link evidence to an existing repair and/or warranty replacement without changing the live repair record.
- Reconciliation audit trail.
- Duplicate email/source protection by SHA-256.
- MCPS customer dashboard now shows warranty replacement serials already held in the existing replacement stock table (gated to client company names containing `MCPS` or `Montgomery`).

## Important safety behavior
Reconciliation imports do **not** overwrite live repair records. They create evidence records and links for review.

## Deployment
`extract-msg==0.55.0` was added to `requirements.txt` so Outlook `.msg` files can be parsed on Render.
