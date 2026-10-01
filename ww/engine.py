"""WW Points Calculator — engine core.

The scoring engine for the WW "Points" program (current program as of 2026,
NOT the older SmartPoints model). A five-term linear model on food macros,
rounded to the nearest integer and floored at 0, with a ZeroPoint override
for foods on the user's personalized ZeroPoint list.

The default coefficients (V4_ASHLEY) were reverse-engineered and validated
(26/27 exact, including 5 out-of-sample fast-food items) for one WW profile.
Because every WW member's profile differs, the coefficients live in an
editable Profile object that the calibration wizard can rewrite.

    raw    =   c_cal*calories
             + c_satfat*saturated_fat
             + c_sugar*added_sugar
             + c_protein*protein
             + c_fiber*fiber
    Points = max(0, round(raw))      # Python round(): round-half-to-even
    Points = 0 if the food is a ZeroPoint food

All macros are in grams; calories as-is. Rounding is applied ONCE, at the
final value — never per-ingredient — so recipe math has no rounding pile-up.

*** FROZEN (Brad, 2026-09-30) ***
The V4 coefficients, the rounding rule (round-half-to-even, then floor at 0)
and the ZeroPoint override (= 0) are LOCKED. Do not re-fit, re-round or
"improve" them. Reviewers may touch docs/typing/loading only; math concerns go
to Brad as notes. Input validation (finite, non-negative numbers) is done at
the boundaries (ww/inputs.py, Profile.from_dict, calibration, storage) so the
scoring functions below never need to change.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List


# ---------------------------------------------------------------------------
# Coefficient profile
# ---------------------------------------------------------------------------

# The five macro levers, in canonical order. UI and storage rely on this order.
MACRO_KEYS = ("calories", "saturated_fat", "added_sugar", "protein", "fiber")

# The validated V4 coefficients (Ashley's profile). Re-fit 2026-09-30 by
# minimizing exact rounding misses over the full 27-point dataset (22 fit
# points + 5 out-of-sample). Result: 26/27 exact. The only miss is Metamucil
# 4 tsp (a PURE fiber supplement) — WW's fiber handling is non-linear for pure
# supplements, so this is a known model limit, not a fit error. Every real
# food, including all 5 out-of-sample fast-food items, scores exactly.
#
# (V3, the prior hand-tuned fit, missed 3: Metamucil 4tsp, McD large fries,
# and the Crunchwrap Supreme — the latter two were real foods off by 1.)
V4_ASHLEY: Dict[str, float] = {
    "calories": 0.03034,
    "saturated_fat": 0.36869,
    "added_sugar": 0.18540,
    "protein": -0.07317,
    "fiber": -0.11087,
}

# DEPRECATED alias kept only so old imports (`from ww import V3_ASHLEY`) keep
# working. Despite the name it holds the V4 values. New code: use V4_ASHLEY.
V3_ASHLEY = V4_ASHLEY

# The ZeroPoint list a brand-new profile starts with.
DEFAULT_ZERO_POINT_FOODS = (
    "egg",
    "plain baked potato",
    "lean beef steak",
    "plain rolled oats",
    "plain nonfat greek yogurt",
    "tuna in water",
)


def _is_real_number(v) -> bool:
    """True for finite int/float values; rejects bool, NaN, +/-inf, strings."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


@dataclass
class Profile:
    """A named set of coefficients plus the user's ZeroPoint food list.

    coeffs: the five linear coefficients (keys == MACRO_KEYS).
    zero_point_foods: lowercased food names that always score 0 regardless
        of macros (the user's personalized ZeroPoint list).
    """

    name: str = "Ashley (v4)"
    coeffs: Dict[str, float] = field(default_factory=lambda: dict(V4_ASHLEY))
    zero_point_foods: List[str] = field(
        default_factory=lambda: list(DEFAULT_ZERO_POINT_FOODS)
    )

    def is_zero_point(self, food_name: str) -> bool:
        """True if food_name matches a ZeroPoint entry (case-insensitive)."""
        if not food_name:
            return False
        return food_name.strip().lower() in {z.lower() for z in self.zero_point_foods}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        """Build a Profile from stored JSON, defensively.

        - d must be a dict (else TypeError — storage quarantines the file).
        - Coefficients: only known macro keys; only finite real numbers (bool,
          NaN, inf and strings are rejected); any gap falls back to V4.
        - zero_point_foods: a list of strings; a bare string is treated as a
          one-item list (not split into characters); a missing/invalid value
          falls back to the default ZeroPoint list.
        """
        if not isinstance(d, dict):
            raise TypeError(f"profile data must be an object, got {type(d).__name__}")
        coeffs = dict(V4_ASHLEY)
        raw_coeffs = d.get("coeffs")
        if not isinstance(raw_coeffs, dict):
            raw_coeffs = {}
        for k in MACRO_KEYS:
            v = raw_coeffs.get(k)
            if _is_real_number(v):
                coeffs[k] = float(v)
        zp = d.get("zero_point_foods")
        if isinstance(zp, str):
            zp = [zp]
        if isinstance(zp, list):
            zp = [str(z).strip() for z in zp if isinstance(z, str) and z.strip()]
        else:
            zp = list(DEFAULT_ZERO_POINT_FOODS)
        return cls(
            name=str(d.get("name", "Custom")),
            coeffs=coeffs,
            zero_point_foods=zp,
        )


# ---------------------------------------------------------------------------
# Macro container
# ---------------------------------------------------------------------------

@dataclass
class Macros:
    """Macro nutrients for a food or a serving. Grams except calories."""

    calories: float = 0.0
    saturated_fat: float = 0.0
    added_sugar: float = 0.0
    protein: float = 0.0
    fiber: float = 0.0

    def scaled(self, factor: float) -> "Macros":
        """Return macros scaled by a multiplier (used for portions/recipes)."""
        return Macros(
            calories=self.calories * factor,
            saturated_fat=self.saturated_fat * factor,
            added_sugar=self.added_sugar * factor,
            protein=self.protein * factor,
            fiber=self.fiber * factor,
        )

    def __add__(self, other: "Macros") -> "Macros":
        return Macros(
            calories=self.calories + other.calories,
            saturated_fat=self.saturated_fat + other.saturated_fat,
            added_sugar=self.added_sugar + other.added_sugar,
            protein=self.protein + other.protein,
            fiber=self.fiber + other.fiber,
        )

    @classmethod
    def zero(cls) -> "Macros":
        return cls()


# ---------------------------------------------------------------------------
# Core scoring
# ---------------------------------------------------------------------------

def raw_points(macros: Macros, profile: Profile | None = None) -> float:
    """The un-rounded linear score. Used by the calibration fit and tests."""
    p = profile or Profile()
    c = p.coeffs
    return (
        c["calories"] * macros.calories
        + c["saturated_fat"] * macros.saturated_fat
        + c["added_sugar"] * macros.added_sugar
        + c["protein"] * macros.protein
        + c["fiber"] * macros.fiber
    )


def points(
    macros: Macros,
    profile: Profile | None = None,
    *,
    zero_point: bool = False,
) -> int:
    """Final WW points: rounded, floored at 0, with ZeroPoint override.

    zero_point: pass True to force 0 (food is on the ZeroPoint list). The
    caller (single-food tab) decides this via the toggle; recipe mode passes
    zero_point per-ingredient through Ingredient.is_zero.
    """
    if zero_point:
        return 0
    return max(0, round(raw_points(macros, profile)))


def points_for_food(
    food_name: str,
    macros: Macros,
    profile: Profile | None = None,
) -> int:
    """Convenience: resolve ZeroPoint by name against the profile, then score."""
    p = profile or Profile()
    return points(macros, p, zero_point=p.is_zero_point(food_name))


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    prof = Profile()
    big_mac = Macros(calories=590, saturated_fat=11, added_sugar=7, protein=25, fiber=3)
    print("Big Mac:", points(big_mac, prof), "(expected 21)")
