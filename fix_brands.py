"""One-off cleanup: repair units whose brand duplicates the model.

Some units were imported with the model name in the brand field, giving
display names like "MIMIOPRO MIMIOPRO 754". This sets the brand to Boxlight
where the existing brand is clearly a model designation.

Run from the Render shell:
    python fix_brands.py           # dry run, shows what would change
    python fix_brands.py --apply   # writes the changes
"""
import sys
from app import app, db, Unit

MODEL_WORDS = {"MIMIOPRO", "PROCOLOR", "PROCOLOUR"}
CORRECT_BRAND = "Boxlight"

with app.app_context():
    units = Unit.query.order_by(Unit.id).all()
    changes = []

    for u in units:
        brand = (u.brand or "").strip()
        model = (u.model or "").strip()
        if not brand:
            continue
        # Brand is a model designation, or brand is repeated at the start of model
        looks_wrong = (
            brand.upper() in MODEL_WORDS
            or model.upper().startswith(brand.upper() + " ")
        )
        if looks_wrong and brand.lower() != CORRECT_BRAND.lower():
            changes.append((u, brand, model))

    if not changes:
        print("Nothing to change — no units have a brand duplicating the model.")
        sys.exit(0)

    print(f"{len(changes)} unit(s) would change:\n")
    for u, brand, model in changes:
        print(f"  {u.intake_id:12s}  '{brand} {model}'  ->  '{CORRECT_BRAND} {model}'")

    if "--apply" not in sys.argv:
        print("\nDry run. Re-run with --apply to write these changes.")
        sys.exit(0)

    for u, _, _ in changes:
        u.brand = CORRECT_BRAND
    db.session.commit()
    print(f"\nUpdated {len(changes)} unit(s).")
