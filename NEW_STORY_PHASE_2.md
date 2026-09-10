# New Story IT — Phase 2

Customer-facing portal redesign and operational visibility layer.

## Added
- Shared professional New Story customer portal shell with left navigation.
- IT service overview dashboard with live request, return, exception and asset counts.
- Searchable/filterable customer request list.
- Searchable/filterable serialized asset inventory.
- Searchable shipment/return history.
- Expanded request detail with requested equipment, assigned serialized devices, shipment history, attention flags and public activity timeline.
- Recent activity and recent shipment feeds on the customer dashboard.

## New customer routes
- `/new-story/portal`
- `/new-story/portal/requests`
- `/new-story/portal/assets`
- `/new-story/portal/shipments`
- `/new-story/portal/requests/<id>`

This phase does not alter the Boxlight/MCPS data model or customer portal workflows.
