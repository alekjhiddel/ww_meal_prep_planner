"""WW Points Calculator — recipe model.

Recipe mode: N ingredients (each with name + brand + macros for the amount
used) sum across the whole pot, then the total is divided by yield and
multiplied by serving size. Because the scoring model is linear, this
whole-recipe calculation is exact — rounding is applied ONCE to the final
per-serving value, so there is no per-ingredient rounding pile-up.

A ZeroPoint ingredient contributes 0: its macros are excluded from the scored
sum (mirrors how WW treats ZeroPoint foods inside a recipe). An ingredient is
ZeroPoint if its ZP box is ticked OR (when a profile is given) its name is on
the profile's ZeroPoint list — the same rule the single-food tab uses, so a
food scores the same on both tabs.

Invalid yield/serving (<= 0 or not finite) raises ValueError — it is never
reported as a silent "0 points", which is indistinguishable from a free recipe.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from .engine import Macros, Profile, points, raw_points


MAX_STORED = 1_000_000.0  # same cap as ww.inputs.MAX_INPUT


# ---------------------------------------------------------------------------
# ZeroPoint NAME matching (shared by the Single-food and Recipe tabs).
# This only decides WHETHER a food is on the user's list; the frozen engine
# rule (a ZeroPoint food scores 0) is applied unchanged by engine.points().
# ---------------------------------------------------------------------------

def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("oes"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _zp_norm(name: str) -> str:
    words = " ".join(str(name or "").split()).casefold().split(" ")
    if words and words[-1]:
        words[-1] = _singular(words[-1])
    return " ".join(words).strip()


def zero_point_match(profile: Optional[Profile], name: str) -> bool:
    """Name is on the profile's ZeroPoint list: exact (any case/spacing) or a
    simple plural of a list entry ("eggs" == "egg", "potatoes" == "potato")."""
    if profile is None or not (name or "").strip():
        return False
    if profile.is_zero_point(name):
        return True
    n = _zp_norm(name)
    return any(_zp_norm(z) == n for z in profile.zero_point_foods)


def zero_point_near_miss(profile: Optional[Profile], name: str) -> Optional[str]:
    """A ZeroPoint list entry whose words all appear in `name` without being an
    exact match ("large egg" vs "egg"). Used only to SHOW a hint — never to
    score 0 automatically (a "fried egg sandwich" is not a ZeroPoint food)."""
    if profile is None or not (name or "").strip() or zero_point_match(profile, name):
        return None
    have = {_singular(w) for w in name.casefold().split()}
    for z in profile.zero_point_foods:
        want = {_singular(w) for w in z.casefold().split()}
        if want and want <= have:
            return z
    return None


def _finite_nonneg(v, what: str) -> float:
    """Coerce a stored value to a finite, non-negative float or raise ValueError."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, bool):
        raise ValueError(f"{what}: not a number")
    f = float(v)  # raises ValueError/TypeError on junk
    if not math.isfinite(f) or f < 0 or f > MAX_STORED:
        raise ValueError(f"{what}: must be a number from 0 to {MAX_STORED:g} (got {v!r})")
    return f


@dataclass
class Ingredient:
    """One ingredient line in a recipe.

    name/brand: identity (brand required by spec for the shopping list).
    macros: the macros for the amount of this ingredient USED in the recipe
        (not per-serving of the ingredient — the amount that went in the pot).
    is_zero: True if this ingredient is a ZeroPoint food (contributes 0).
    """

    name: str
    brand: str = ""
    macros: Macros = field(default_factory=Macros.zero)
    is_zero: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Ingredient":
        """Raises ValueError/TypeError on malformed data (storage handles it)."""
        if not isinstance(d, dict):
            raise TypeError("ingredient must be an object")
        m = d.get("macros") or {}
        if not isinstance(m, dict):
            raise TypeError("ingredient macros must be an object")
        return cls(
            name=str(d.get("name", "") or ""),
            brand=str(d.get("brand", "") or ""),
            macros=Macros(
                calories=_finite_nonneg(m.get("calories", 0), "calories"),
                saturated_fat=_finite_nonneg(m.get("saturated_fat", 0), "saturated_fat"),
                added_sugar=_finite_nonneg(m.get("added_sugar", 0), "added_sugar"),
                protein=_finite_nonneg(m.get("protein", 0), "protein"),
                fiber=_finite_nonneg(m.get("fiber", 0), "fiber"),
            ),
            is_zero=bool(d.get("is_zero", False)),
        )


@dataclass
class Recipe:
    """A saved recipe: ingredients + total yield + serving size.

    yield_amount: total amount the recipe produces (e.g. 64 oz of soup).
    serving_size: the size of one serving in the same unit (e.g. 8 oz).
    unit: free-text unit label for display (e.g. "oz", "g", "cup").
    """

    name: str = ""
    ingredients: List[Ingredient] = field(default_factory=list)
    yield_amount: float = 1.0
    serving_size: float = 1.0
    unit: str = "serving"

    # -- scoring -----------------------------------------------------------

    def is_zero_ingredient(self, ing: Ingredient, profile: Profile | None = None) -> bool:
        """ZP box ticked, or name on the profile's ZeroPoint list."""
        return bool(ing.is_zero) or zero_point_match(profile, ing.name)

    def scored_macros_total(self, profile: Profile | None = None) -> Macros:
        """Sum macros across all NON-ZeroPoint ingredients (the scored pot)."""
        total = Macros.zero()
        for ing in self.ingredients:
            if not self.is_zero_ingredient(ing, profile):
                total = total + ing.macros
        return total

    def servings(self) -> float:
        """Number of servings = yield / serving size. Raises ValueError if invalid."""
        y, s = self.yield_amount, self.serving_size
        for label, v in (("Total yield", y), ("Serving size", s)):
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
                raise ValueError(f"{label} must be a number.")
            if v <= 0:
                raise ValueError(f"{label} must be more than 0.")
        return y / s

    def points_per_serving(self, profile: Profile | None = None) -> int:
        """Points for one serving — rounded ONCE at the end."""
        n = self.servings()
        per_serving = self.scored_macros_total(profile).scaled(1.0 / n)
        return points(per_serving, profile)

    def raw_points_per_serving(self, profile: Profile | None = None) -> float:
        """Un-rounded points per serving (for display of the exact value)."""
        n = self.servings()
        per_serving = self.scored_macros_total(profile).scaled(1.0 / n)
        return max(0.0, raw_points(per_serving, profile))

    # -- shopping list -----------------------------------------------------

    def shopping_list(self) -> List[str]:
        """Human-readable shopping list: brand + name for every ingredient."""
        items = []
        for ing in self.ingredients:
            label = f"{ing.brand} {ing.name}".strip()
            if label:
                items.append(label)
        return items

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ingredients": [i.to_dict() for i in self.ingredients],
            "yield_amount": self.yield_amount,
            "serving_size": self.serving_size,
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Recipe":
        """Raises ValueError/TypeError on malformed data (storage handles it).

        A stored yield/serving of 0 is loaded as-is (not silently turned into
        1); the UI then shows the "must be more than 0" error.
        """
        if not isinstance(d, dict):
            raise TypeError("recipe must be an object")
        ings = d.get("ingredients", [])
        if not isinstance(ings, list):
            raise TypeError("ingredients must be a list")
        return cls(
            name=str(d.get("name", "") or ""),
            ingredients=[Ingredient.from_dict(i) for i in ings],
            yield_amount=_finite_nonneg(d.get("yield_amount", 1), "yield_amount"),
            serving_size=_finite_nonneg(d.get("serving_size", 1), "serving_size"),
            unit=str(d.get("unit", "serving") or "serving"),
        )
