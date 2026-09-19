"""Static reference data the generator draws from.

Geography is synthetic: `name` and `territory` are invented commercial
groupings, `country` is only ever a country name. There are no addresses, no
coordinates and no personal data anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models.enums import Department


@dataclass(frozen=True, slots=True)
class RegionSpec:
    """A sales region, with the commercial parameters attached to it."""

    name: str
    country: str
    territory: str
    #: Relative share of demand. Regions are deliberately unequal so that
    #: "which regions underperform?" has a real answer.
    demand_weight: float
    #: Sales tax applied to the discounted subtotal.
    tax_rate: Decimal


REGIONS: tuple[RegionSpec, ...] = (
    # North America — the largest book.
    RegionSpec("Pacific Northwest", "United States", "North America", 1.00, Decimal("0.088")),
    RegionSpec("Great Lakes", "United States", "North America", 1.22, Decimal("0.071")),
    RegionSpec("Northeast Corridor", "United States", "North America", 1.55, Decimal("0.0825")),
    RegionSpec("Sun Belt", "United States", "North America", 1.34, Decimal("0.0625")),
    RegionSpec("Prairie Provinces", "Canada", "North America", 0.46, Decimal("0.05")),
    # EMEA.
    RegionSpec("British Isles", "United Kingdom", "EMEA", 0.93, Decimal("0.20")),
    RegionSpec("DACH", "Germany", "EMEA", 1.11, Decimal("0.19")),
    RegionSpec("Benelux", "Netherlands", "EMEA", 0.58, Decimal("0.21")),
    RegionSpec("Iberia", "Spain", "EMEA", 0.52, Decimal("0.21")),
    RegionSpec("Nordics", "Sweden", "EMEA", 0.44, Decimal("0.25")),
    # APAC — smallest but fastest growing.
    RegionSpec("ANZ", "Australia", "APAC", 0.61, Decimal("0.10")),
    RegionSpec("Southeast Asia", "Singapore", "APAC", 0.49, Decimal("0.08")),
)


@dataclass(frozen=True, slots=True)
class CategorySpec:
    """A product category, its subcategories and its price band."""

    name: str
    subcategories: tuple[str, ...]
    #: Inclusive list-price band, in whole currency units.
    price_low: Decimal
    price_high: Decimal
    #: Gross margin. Cost price is derived from list price and this.
    margin: float
    #: Relative share of the catalogue.
    catalogue_share: float


CATEGORIES: tuple[CategorySpec, ...] = (
    CategorySpec(
        "Consumer Electronics",
        ("Audio", "Wearables", "Displays", "Accessories", "Cameras"),
        Decimal("14"),
        Decimal("890"),
        0.31,
        0.17,
    ),
    CategorySpec(
        "Home & Kitchen",
        ("Cookware", "Small Appliances", "Storage", "Textiles"),
        Decimal("8"),
        Decimal("260"),
        0.44,
        0.15,
    ),
    CategorySpec(
        "Apparel",
        ("Outerwear", "Footwear", "Activewear", "Everyday"),
        Decimal("12"),
        Decimal("185"),
        0.56,
        0.16,
    ),
    CategorySpec(
        "Office Supplies",
        ("Paper", "Desk", "Print & Ink", "Organisation"),
        Decimal("3"),
        Decimal("165"),
        0.38,
        0.12,
    ),
    CategorySpec(
        "Outdoor & Garden",
        ("Power Tools", "Furniture", "Grow", "Leisure"),
        Decimal("11"),
        Decimal("580"),
        0.40,
        0.11,
    ),
    CategorySpec(
        "Health & Fitness",
        ("Cardio", "Strength", "Recovery", "Nutrition"),
        Decimal("9"),
        Decimal("620"),
        0.47,
        0.10,
    ),
    CategorySpec(
        "Industrial Equipment",
        ("Material Handling", "Safety", "Fastening", "Measurement"),
        Decimal("60"),
        Decimal("4200"),
        0.27,
        0.10,
    ),
    CategorySpec(
        "Software & Services",
        ("Licences", "Support Plans", "Training", "Integration"),
        Decimal("35"),
        Decimal("2600"),
        0.78,
        0.09,
    ),
)


#: Short codes used to build readable SKUs.
CATEGORY_CODES: dict[str, str] = {
    "Consumer Electronics": "CE",
    "Home & Kitchen": "HK",
    "Apparel": "AP",
    "Office Supplies": "OS",
    "Outdoor & Garden": "OG",
    "Health & Fitness": "HF",
    "Industrial Equipment": "IE",
    "Software & Services": "SS",
}


#: Word pools for generating plausible product names without a dependency on
#: any external name list.
PRODUCT_NAME_PREFIXES: tuple[str, ...] = (
    "Aurora",
    "Basalt",
    "Cobalt",
    "Drift",
    "Ember",
    "Fathom",
    "Granite",
    "Harbor",
    "Ironwood",
    "Juniper",
    "Kestrel",
    "Lumen",
    "Meridian",
    "Nimbus",
    "Onyx",
    "Pioneer",
    "Quarry",
    "Ridgeline",
    "Summit",
    "Tundra",
    "Vantage",
    "Westward",
    "Zenith",
    "Alloy",
    "Beacon",
    "Cascade",
)

PRODUCT_NAME_SUFFIXES: tuple[str, ...] = (
    "Pro",
    "Max",
    "Lite",
    "Compact",
    "Elite",
    "Studio",
    "Field",
    "Core",
    "Plus",
    "Series 2",
    "Series 3",
    "XL",
    "Mini",
    "Duo",
    "Edge",
)


#: Department headcount shares and the roles within each.
DEPARTMENT_ROLES: dict[str, tuple[str, ...]] = {
    Department.SALES: (
        "Account Executive",
        "Sales Development Rep",
        "Regional Sales Manager",
        "Solutions Consultant",
    ),
    Department.CUSTOMER_SUCCESS: (
        "Customer Success Manager",
        "Support Engineer",
        "Onboarding Specialist",
    ),
    Department.MARKETING: (
        "Demand Generation Manager",
        "Content Strategist",
        "Marketing Analyst",
    ),
    Department.OPERATIONS: (
        "Operations Analyst",
        "Logistics Coordinator",
        "Warehouse Supervisor",
        "Procurement Specialist",
    ),
    Department.FINANCE: (
        "Financial Analyst",
        "Accountant",
        "Revenue Operations Manager",
    ),
    Department.ENGINEERING: (
        "Software Engineer",
        "Data Engineer",
        "Platform Engineer",
        "Engineering Manager",
    ),
}

DEPARTMENT_SHARES: dict[str, float] = {
    Department.SALES: 0.30,
    Department.CUSTOMER_SUCCESS: 0.20,
    Department.OPERATIONS: 0.18,
    Department.ENGINEERING: 0.17,
    Department.MARKETING: 0.09,
    Department.FINANCE: 0.06,
}


#: Free-shipping threshold and the flat rate charged below it.
FREE_SHIPPING_THRESHOLD = Decimal("150.00")
SHIPPING_RATES: tuple[Decimal, ...] = (
    Decimal("4.99"),
    Decimal("7.99"),
    Decimal("12.99"),
    Decimal("19.99"),
)
