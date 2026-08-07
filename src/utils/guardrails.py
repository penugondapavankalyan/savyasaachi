"""
LLM Guardrails — input sanitisation for all MCP tool calls.

The LLM may hallucinate placeholder values for optional fields.
These helpers normalise such values to None/safe defaults before
any DB operation is attempted.

Rules:
  - Never invent values — only process what the user explicitly said
  - If a value was not provided by the user, it must be None
  - Numeric fields must be positive where required
  - Enum fields must be from the allowed set
  - String IDs must look like UUIDs — reject obvious placeholders

Quantity rules by unit:
  - KG, L          → float allowed (0.5 KG sugar, 0.25 L oil are valid)
  - G, ML          → integer only  (smallest atomic unit at a kirana)
  - PACKET, PIECE,
    DOZEN, BUNDLE  → integer only  (items come in whole packs/pieces)
"""

from __future__ import annotations

import re
import uuid
from typing import Optional

# ── Placeholder strings the LLM commonly hallucinates ────────────────────────
_NULL_STRINGS: frozenset[str] = frozenset({
    "", "none", "null", "n/a", "na", "no", "not provided", "not given",
    "unknown", "undefined", "unspecified", "not applicable", "not available",
    "not set", "not specified", "empty", "skip", "skipped", "n", "no gstin",
    "no gst", "no hsn", "some_gstin", "some_hsn", "some_value", "placeholder",
    "example", "test", "xxx", "yyy", "zzz", "0000", "1234", "abcd",
})

# Valid UUID pattern
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Valid Indian state codes
_VALID_STATE_CODES: frozenset[str] = frozenset({
    "01", "02", "03", "04", "05", "06", "07", "08", "09", "10",
    "18", "19", "20", "21", "22", "23", "24", "27", "29", "30",
    "32", "33", "36", "37",
})

# Valid GST rates for kirana items
_VALID_GST_RATES: frozenset[float] = frozenset({0.0, 5.0, 12.0, 18.0, 28.0})

# Valid units
_VALID_UNITS: frozenset[str] = frozenset({
    "KG", "G", "L", "ML", "PACKET", "PIECE", "DOZEN", "BUNDLE",
})

# Alias map: common synonyms → canonical unit name
# The LLM (and owners) often use shorthand that must be normalised
# before validation. All keys must be UPPERCASE.
_UNIT_ALIASES: dict[str, str] = {
    # Weight
    "KILOGRAM": "KG",
    "KILOGRAMS": "KG",
    "KILO": "KG",
    "KILOS": "KG",
    "GRAM": "G",
    "GRAMS": "G",
    "GMS": "G",
    "GM": "G",
    # Volume
    "LITRE": "L",
    "LITRES": "L",
    "LITER": "L",
    "LITERS": "L",
    "LTR": "L",
    "LTS": "L",
    "MILLILITRE": "ML",
    "MILLILITRES": "ML",
    "MILLILITER": "ML",
    "MILLILITERS": "ML",
    "MLS": "ML",
    # Packet / Pack
    "PACK": "PACKET",
    "PACKS": "PACKET",
    "PACKETS": "PACKET",
    "PKT": "PACKET",
    "PKTS": "PACKET",
    "POUCH": "PACKET",
    "BAG": "PACKET",
    "SACHET": "PACKET",
    # Piece / Unit
    "PIECES": "PIECE",
    "PC": "PIECE",
    "PCS": "PIECE",
    "UNIT": "PIECE",
    "UNITS": "PIECE",
    "NOS": "PIECE",
    "NO": "PIECE",
    "NUMBER": "PIECE",
    "NUMBERS": "PIECE",
    "ITEM": "PIECE",
    "ITEMS": "PIECE",
    "EA": "PIECE",
    "EACH": "PIECE",
    "BOX": "PIECE",
    "BOTTLE": "PIECE",
    "BOTTLES": "PIECE",
    "CAN": "PIECE",
    "CANS": "PIECE",
    "JAR": "PIECE",
    "JARS": "PIECE",
    "TIN": "PIECE",
    "TINS": "PIECE",
    "TUBE": "PIECE",
    # Dozen
    "DOZENS": "DOZEN",
    "DZ": "DOZEN",
    "DZN": "DOZEN",
    # Bundle
    "BUNDLES": "BUNDLE",
    "BUNCH": "BUNDLE",
    "BUNCHES": "BUNDLE",
    "ROLL": "BUNDLE",
    "ROLLS": "BUNDLE",
}

# Valid payment modes
_VALID_PAYMENT_MODES: frozenset[str] = frozenset({"CASH", "UPI", "CARD", "CREDIT"})

# ── Quantity rules by unit ────────────────────────────────────────────────────
# Units that require WHOLE-NUMBER quantities only.
# Rationale:
#   PACKET / PIECE / DOZEN / BUNDLE — sold as discrete sealed units, can't split.
#   G / ML — smallest atomic unit at a kirana; nobody sells 0.5g or 0.5ml.
# Units that allow FLOAT quantities:
#   KG / L — sold by weight/volume; 0.5 KG or 0.25 L is perfectly valid.
_INTEGER_ONLY_UNITS: frozenset[str] = frozenset({
    "PACKET", "PIECE", "DOZEN", "BUNDLE", "G", "ML",
})
_FLOAT_ALLOWED_UNITS: frozenset[str] = frozenset({"KG", "L"})

# Human-readable explanation per unit (shown in error messages)
_UNIT_REASON: dict[str, str] = {
    "PACKET": "packets are sold as whole sealed units",
    "PIECE":  "pieces are sold as whole units",
    "DOZEN":  "dozens are sold as whole sets",
    "BUNDLE": "bundles are sold as whole units",
    "G":      "grams are the smallest unit — use whole grams",
    "ML":     "millilitres are the smallest unit — use whole millilitres",
}


# ── Core sanitisers ───────────────────────────────────────────────────────────

def clean_optional_str(value: Optional[str]) -> Optional[str]:
    """Return None if value is empty/placeholder, else return stripped value."""
    if value is None:
        return None
    v = value.strip()
    if v.lower() in _NULL_STRINGS:
        return None
    return v


def clean_gstin(value: Optional[str]) -> Optional[str]:
    """Sanitise GSTIN — return None if not a real 15-char GSTIN."""
    v = clean_optional_str(value)
    if v is None:
        return None
    # Must be exactly 15 chars and match the pattern
    pattern = re.compile(
        r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"
    )
    if pattern.match(v.upper()):
        return v.upper()
    return None  # invalid format → treat as not provided


def clean_hsn_code(value: Optional[str]) -> Optional[str]:
    """Sanitise HSN code — must be 4–8 digits, else None."""
    v = clean_optional_str(value)
    if v is None:
        return None
    # HSN codes are 4–8 numeric digits
    if re.match(r"^\d{4,8}$", v):
        return v
    return None


def clean_phone(value: Optional[str]) -> Optional[str]:
    """
    Sanitise and validate an Indian mobile phone number.

    Rules:
    - Strip spaces, dashes, +, (, ) and the +91 / 0 prefix.
    - After stripping, must be exactly 10 digits.
    - Must start with 6, 7, 8 or 9 (valid Indian mobile prefixes).
    - Returns None if invalid (caller must raise an appropriate error).
    """
    v = clean_optional_str(value)
    if v is None:
        return None
    # Strip all non-digit characters
    digits = re.sub(r"[^\d]", "", v)
    # Remove country code prefix: +91 → 91 (already stripped to digits)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    # Remove leading 0 (STD-style local dialling)
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    # Must be exactly 10 digits and start with 6/7/8/9
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return None


def clean_uuid(value: Optional[str]) -> Optional[str]:
    """Return value only if it looks like a real UUID, else None."""
    v = clean_optional_str(value)
    if v is None:
        return None
    if _UUID_RE.match(v):
        return v.lower()
    return None


def clean_positive_float(value: Optional[float], field_name: str = "value") -> float:
    """Raise ValueError if value is None or <= 0."""
    if value is None:
        raise ValueError(f"{field_name} is required and must be a positive number.")
    if value <= 0:
        raise ValueError(f"{field_name} must be positive, got {value}.")
    return float(value)


def clean_non_negative_float(value: Optional[float], field_name: str = "value") -> float:
    """Raise ValueError if value is None or < 0."""
    if value is None:
        raise ValueError(f"{field_name} is required and must be 0 or positive.")
    if value < 0:
        raise ValueError(f"{field_name} must be 0 or positive, got {value}.")
    return float(value)


# Valid GST slabs for branded items (0% is NOT allowed for branded)
_VALID_BRANDED_GST_RATES: frozenset[float] = frozenset({5.0, 12.0, 18.0, 28.0})


def clean_gst_rate(value: float, is_loose: bool) -> float:
    """
    Enforce GST rate rules:
    - Loose items: always 0% (forced regardless of input)
    - Branded items: MUST be exactly one of 5 / 12 / 18 / 28 %
      - 0% raises ValueError → agent must ask owner for the rate.
      - Values outside the valid slab set raise ValueError → agent must
        ask owner for the correct rate. We do NOT silently snap to nearest
        because a wrong GST rate means wrong billing and wrong tax filings.

    Raises:
        ValueError — if branded item is given 0% or an invalid rate
    """
    if is_loose:
        return 0.0

    rate = float(value)

    # 0% for a branded/packaged item is almost certainly a missing input.
    if rate == 0.0:
        raise ValueError(
            "GST rate is required for branded/packaged items. "
            "Please ask the owner: 'What is the GST rate? (5 / 12 / 18 / 28 %)'"
        )

    # Exact match — accept immediately.
    if rate in _VALID_BRANDED_GST_RATES:
        return rate

    # Invalid rate — reject clearly. Silently snapping to nearest slab
    # would produce wrong billing and wrong GST filings.
    raise ValueError(
        f"Invalid GST rate {rate}% for a branded item. "
        f"Valid rates are: 5%, 12%, 18%, 28%. "
        f"Please ask the owner: 'What is the GST rate? (5 / 12 / 18 / 28 %)'"
    )


def clean_unit(value: str) -> str:
    """
    Normalise unit to uppercase, resolve common aliases, then validate.

    Owners and the LLM commonly use shorthand like 'kg', 'pack', 'pcs',
    'ltr', 'nos', 'bottle' etc.  These are all mapped to their canonical
    UNIT value before validation so the guardrail never fires on a valid
    synonym.

    Raises ValueError if the value cannot be resolved to a known unit.
    """
    if not value:
        raise ValueError(
            "Unit is required. Must be one of: BUNDLE, DOZEN, G, KG, L, ML, PACKET, PIECE"
        )
    v = value.strip().upper()
    # Exact match first
    if v in _VALID_UNITS:
        return v
    # Try alias lookup
    canonical = _UNIT_ALIASES.get(v)
    if canonical:
        return canonical
    raise ValueError(
        f"Invalid unit '{value}'. Must be one of: {', '.join(sorted(_VALID_UNITS))}. "
        f"Common aliases accepted: kg, g, l/ltr, ml, pack/pkt/pouch, "
        f"piece/pc/pcs/nos/bottle/box/jar/can, dozen/dz, bundle/bunch/roll."
    )


def clean_payment_mode(value: Optional[str], default: str = "CASH") -> str:
    """Normalise payment mode — fallback to default if not recognised."""
    v = clean_optional_str(value)
    if v is None:
        return default
    upper = v.upper()
    if upper in _VALID_PAYMENT_MODES:
        return upper
    return default


def clean_state_code(value: Optional[str], default: str = "29") -> str:
    """Validate Indian state code — fallback to Karnataka (29) if invalid."""
    v = clean_optional_str(value)
    if v is None:
        return default
    # Normalise: zero-pad single-digit codes
    if v.isdigit():
        v = v.zfill(2)
    if v in _VALID_STATE_CODES:
        return v
    return default


def clean_brand(value: Optional[str]) -> Optional[str]:
    """
    Brand name sanitiser.
    Returns None if no brand was explicitly stated (loose items, generic items).
    """
    return clean_optional_str(value)


def clean_name(value: str, field_name: str = "name") -> str:
    """Ensure name is non-empty after stripping."""
    v = value.strip() if value else ""
    if not v or v.lower() in _NULL_STRINGS:
        raise ValueError(f"{field_name} is required and cannot be empty.")
    return v


def clean_quantity_for_unit(
    quantity: float,
    unit: str,
    field_name: str = "quantity",
) -> float:
    """
    Validate that quantity is appropriate for the given unit.

    Rules:
      KG, L          → float allowed  (e.g. 0.5 KG sugar, 0.25 L oil)
      G, ML          → integer only   (e.g. 200 G, 500 ML — not 200.5 G)
      PACKET, PIECE,
      DOZEN, BUNDLE  → integer only   (e.g. 2 packets — not 1.5 packets)

    Also enforces positivity (quantity > 0).

    Raises:
        ValueError with a clear human-readable message on any violation.

    Returns:
        float — the validated quantity (always a whole number for integer-only units)
    """
    # Normalise unit
    u = unit.strip().upper() if unit else ""

    # Step 1 — must be positive
    if quantity is None or quantity <= 0:
        raise ValueError(f"{field_name} must be a positive number, got {quantity}.")

    qty = float(quantity)

    # Step 2 — integer-only units: reject fractional quantities
    if u in _INTEGER_ONLY_UNITS:
        if qty != int(qty):
            reason = _UNIT_REASON.get(u, f"{u} items are sold as whole units")
            raise ValueError(
                f"Invalid quantity {qty} for unit {u}: {reason}. "
                f"Please enter a whole number (e.g. {int(qty) or 1} or {int(qty) + 1})."
            )
        return float(int(qty))   # normalise 2.0 → 2.0 (no trailing decimals issue)

    # Step 3 — float-allowed units: just ensure positive (already checked above)
    # KG and L: up to 3 decimal places is sensible (e.g. 0.250 KG)
    if u in _FLOAT_ALLOWED_UNITS:
        # Round to 3 decimal places to avoid floating-point artefacts
        return round(qty, 3)

    # Unknown unit — shouldn't reach here if clean_unit() was called first,
    # but be safe: allow the quantity through (unit validation already failed elsewhere)
    return round(qty, 3)


def is_integer_unit(unit: str) -> bool:
    """
    Return True if the given unit requires whole-number quantities only.
    Useful for system-prompt generation and LLM guidance.
    """
    return unit.strip().upper() in _INTEGER_ONLY_UNITS
