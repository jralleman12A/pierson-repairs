# New Story Phase 5 — Client Portal Refinement

This pass focuses on making the New Story customer portal useful for day-to-day inventory questions rather than simply reporting total asset ownership.

## Inventory changes
- Replaced the small "By Category" sidebar with a full operational inventory matrix.
- Added **At Pierson** as a first-class custody count.
- Added columns for Total Owned, At Pierson, Available, Reserved/Staging, Deployed/Out, Return Pending, and Repair.
- Every summary number drills into the matching asset list.
- Added lifecycle-view filtering in the asset search area.
- Search now includes assigned person, room and current location in addition to serial, asset tag, model and PO.
- Added last-activity and PO columns to the detail table.
- Added a separate bulk/non-serialized stock section so quantity-managed accessories do not inflate serialized asset ownership totals.
- Top locations now focuses on deployed/outbound assets rather than mixing warehouse inventory with field inventory.

## Dashboard changes
- The customer overview now calls out **At Pierson** instead of the ambiguous "Available Assets" metric.
- Total Owned and At Pierson metrics link directly into inventory.

## Pierson custody definition
Assets are counted as physically at Pierson when their lifecycle status is one of:
Received, Available, Reserved, Allocated, Processing, Ready to Ship, Returned, or Repair.

Expected inventory is not counted until received. Shipped/Deployed/In Use/Return Pending is considered outside Pierson custody. Retired/Scrapped/Lost is excluded from Pierson stock.
