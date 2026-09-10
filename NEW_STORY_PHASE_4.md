# New Story Phase 4 — Operational Inventory Management

Phase 4 turns New Story inventory from a read-only asset list into an operational IT asset/stock control system.

## Added

- Inventory Control dashboard with serialized/on-hand/deployed/exception counts
- Serialized asset register with status, location, assignee, room, ticket and bulk actions
- Asset detail page with editable lifecycle state and movement history
- Scanner console for lookup, receiving, reserve, allocate, stage, deploy, return, repair, lost and scrap workflows
- Quantity-stock model for non-serialized accessories
- Quantity stock adjustments and reorder thresholds
- Inventory movement/audit ledger
- Shipment batch builder: create batch, scan exact assets, remove mistakes, then close shipment
- Direction-aware inbound return receiving
- New Story customer asset inventory dashboard with category/status summaries, current location, assignee/room and clickable lifecycle history
- Additional asset statuses: Reserved, In Use and Lost

## Safety / integrity improvements

- A shipment no longer auto-attaches every allocated device on a request.
- Exact serials/asset tags must be scanned into a shipment batch before it can be closed.
- Unknown scans stop with an explicit error instead of silently disappearing.
- Every new inventory movement records actor, previous state, new state, request/shipment context and notes.
- Existing legacy asset columns are widened/added automatically on startup migrations.

## Existing data

The Phase 3 legacy seed remains included. Run `/new-story/legacy-import` after deployment if the legacy import has not completed yet.
Existing legacy assets will appear in the new inventory screens immediately after import. Movement history begins when those assets are acted on in the new system.
