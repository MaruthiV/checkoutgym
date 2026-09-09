ITEMS = {
    "item_tee": {"name": "Cotton tee", "unit_amount": 1200},
    "item_hoodie": {"name": "Heavyweight hoodie", "unit_amount": 4800},
    "item_jacket": {"name": "Waxed jacket", "unit_amount": 18000},
    "item_bundle": {"name": "Tee + hoodie bundle", "unit_amount": 5500},
    "item_preorder": {"name": "Pre-order cap (ships in 6 weeks)", "unit_amount": 3000, "preorder": True},
    "item_print": {"name": "Region-locked art print", "unit_amount": 2500, "region_restricted": True},
    "item_socks": {"name": "Wool socks (3 pack)", "unit_amount": 3598},
    "item_beanie": {"name": "Beanie", "unit_amount": 850},
}

SHIPPING = [
    {"id": "ship_std", "title": "Standard", "description": "Arrives in 5-7 days", "carrier": "USPS", "amount": 599},
    {"id": "ship_exp", "title": "Express", "description": "Arrives in 1-2 days", "carrier": "UPS", "amount": 1999},
]

# s9 only: standard ships each item separately, consolidated is one box
SHIPPING_S9 = [
    {"id": "ship_std", "title": "Standard (ships each item as available)", "description": "5-7 days, separate packages", "carrier": "USPS", "amount": 599, "per_item": True},
    {"id": "ship_consolidated", "title": "Consolidated (one delivery)", "description": "5-7 days, all items in one package", "carrier": "USPS", "amount": 1999, "per_item": False},
]

COUPONS = {"SAVE10": {"percent": 10}}
