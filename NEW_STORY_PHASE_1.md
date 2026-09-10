# New Story IT Management — Phase 1

This build creates a completely separate New Story operational module beside the existing Boxlight/MCPS repair program. No Boxlight repair records are reused for New Story.

## Included now
- Dedicated New Story data model and customer account
- Operations overview and request/ticket queue
- Manual request creation
- ManageEngine email paste → parse → preview/import workflow
- Requested line items and shortage/blocker tracking
- Asset inventory with raw + normalized serial storage
- Legacy serial normalization rules for Chromebook and Windows devices
- Receiving workflow that creates inventory placeholders from received quantity
- Asset allocation to tickets
- Shipment/return records and automatic shipped asset status changes
- Return-kit behavior at the request level
- Exceptions / missing-items queue using real database state instead of orange cell colors
- Location directory
- Chronological activity history on every request
- Read-only New Story customer portal using the unified public sign-in page
- Render bootstrap variables for the first New Story account

## Intentionally not included yet
- Historical spreadsheet migration (the workbook is now legacy input only)
- Automated mailbox connection / polling; Phase 1 uses safe paste-and-import
- Barcode scan screens and bulk scan workflow
- Installation job module
- Non-serialized quantity-ledger optimization
- Shipment notification batches / email notifications
- Advanced reporting and SLA analytics

Those are the next layers once Phase 1 is live and the basic workflow is validated.
