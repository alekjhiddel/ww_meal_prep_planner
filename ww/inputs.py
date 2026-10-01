"""WW Points Calculator — strict, friendly number parsing for user input.

Pure functions (no GUI dependency) so the parsing rules are unit-testable and
shared by every tab. The rule is simple: a box either holds a real, finite
number we understand, or we show an error under the box and refuse to show a
points value. We NEVER silently turn junk into 0 (the v1 behaviour, which
showed confidently wrong numbers for inputs like "1O0").

Accepted:
    ""            -> 0 (an empty box means "none of this macro")
    "12", "12.5", " 12 ", "+12", ".5"
    "1,5"         -> 1.5   (decimal comma, common outside the US)
    "1,234"       -> 1234  (US thousands separator: groups of exactly 3)
    "1,234.5"     -> 1234.5
    "1/2", "1 1/2", "1½", "½" -> fractions (handy for cups / servings)
    a trailing unit the box expects, copied straight off the label:
        "12g", "12 g", "12 grams" in a gram box; "150 cal"/"150 kcal" in the
        calories box; "9 pts" in a Points box; "8 oz", "2 cups" in a
        yield/serving box. Only the units passed in `units=` are stripped —
        "12 oz" in a gram box is still an error, never silently "12".
Rejected (InputError with a plain-English message):
    "1O0", "abc", "1.2.3", "nan", "inf", "1e999"(overflow), "1,2,3", "1/0"
    anything larger than MAX_INPUT (1,000,000)
    negative numbers (macros/servings can't be negative)
"""

from __future__ import annotations

import math
import re

_THOUSANDS = re.compile(r"^[+]?\d{1,3}(,\d{3})+(\.\d+)?$")
_DECIMAL_COMMA = re.compile(r"^[+-]?\d*,\d+$")
_PLAIN = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_FRACTION = re.compile(r"^(?:(\d+)\s+)?(\d+)\s*/\s*(\d+)$")
_TRAILING_WORD = re.compile(r"^(.*?\d)\s*([A-Za-z][A-Za-z.]*)$")
_VULGAR = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4",
           "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8"}

# Units each kind of box understands (lower-case, '.' removed before compare).
GRAM_UNITS = ("g", "gr", "gram", "grams")
CALORIE_UNITS = ("cal", "cals", "kcal", "kcals", "calorie", "calories")
POINT_UNITS = ("pt", "pts", "point", "points")
AMOUNT_UNITS = ("serving", "servings", "portion", "portions", "oz", "ounce", "ounces",
                "g", "gram", "grams", "kg", "lb", "lbs", "pound", "pounds",
                "cup", "cups", "c", "tbsp", "tbs", "tablespoon", "tablespoons",
                "tsp", "teaspoon", "teaspoons", "ml", "l", "liter", "liters", "litre", "litres",
                "floz", "piece", "pieces", "pc", "pcs", "slice", "slices", "bowl", "bowls",
                "can", "cans", "jar", "jars", "each", "x")


# Largest number any box accepts. A whole pot of food is well under this; the
# cap stops absurd values (1e308) overflowing the maths into an error.
MAX_INPUT = 1_000_000.0


class InputError(ValueError):
    """A user-entered value could not be used. str(e) is UI-ready text."""


def _strip_unit(s: str, units) -> str:
    """'12 g' -> '12' if 'g' is an accepted unit for this box; else unchanged."""
    if not units:
        return s
    m = _TRAILING_WORD.match(s)
    if not m:
        return s
    word = m.group(2).lower().replace(".", "")
    allowed = {u.lower().replace(".", "") for u in units}
    if word in allowed:
        return m.group(1).strip()
    return s


def _bad(text, units) -> InputError:
    hint = f" (a unit like '{units[0]}' is fine)" if units else ""
    return InputError(f"'{text}' isn't a number — use digits, like 12 or 12.5{hint}.")


def parse_number(text, *, allow_negative: bool = False, empty_value: float | None = 0.0,
                 units=()) -> float:
    """Parse one input box. Raises InputError with a friendly message.

    empty_value: what an empty box means. Pass None to make the box required.
    units: unit words this box accepts after the number (stripped, not converted).
    """
    if text is None:
        text = ""
    s = str(text).strip().replace("\u2212", "-")  # unicode minus -> ascii
    if s == "":
        if empty_value is None:
            raise InputError("Required — type a number.")
        return float(empty_value)

    for ch, rep in _VULGAR.items():   # "1½ g" -> "1 1/2 g" (before unit strip)
        s = s.replace(ch, f" {rep}")
    s = " ".join(s.replace("\u2044", "/").split())  # fraction slash; tidy spaces
    s = _strip_unit(s, units)

    fm = _FRACTION.match(s)
    if fm:
        whole, num, den = fm.groups()
        if int(den) == 0:
            raise InputError(f"'{text}' divides by zero.")
        v = (int(whole) if whole else 0) + int(num) / int(den)
    else:
        if _THOUSANDS.match(s):
            s = s.replace(",", "")
        elif _DECIMAL_COMMA.match(s):
            s = s.replace(",", ".")
        if not _PLAIN.match(s):
            raise _bad(text, units)
        try:
            v = float(s)
        except (ValueError, OverflowError):
            raise _bad(text, units)
    if not math.isfinite(v):
        raise InputError(f"'{text}' is too large to use.")
    if abs(v) > MAX_INPUT:
        raise InputError(f"'{text}' is too large — the most any box takes is {MAX_INPUT:,.0f}.")
    if v < 0 and not allow_negative:
        raise InputError("Can't be negative.")
    if v == 0:
        v = 0.0  # normalise -0.0
    return v


def parse_positive(text, *, units=()) -> float:
    """A required number that must be > 0 (recipe yield / serving size)."""
    v = parse_number(text, empty_value=None, units=units)
    if v <= 0:
        raise InputError("Must be more than 0.")
    return v


# Canonical spelling of each amount unit, so "64 oz" and "8 ounces" are
# recognised as the SAME unit and "64 oz" vs "1 cup" as DIFFERENT ones. The
# app never converts between units — it only refuses a mismatch, because
# stripping the words and dividing 64 by 1 would silently give 64 servings.
_UNIT_CANON = {}
for _canon, _words in {
    "serving": ("serving", "servings", "portion", "portions", "each", "x"),
    "oz": ("oz", "ounce", "ounces"),
    "floz": ("floz",),
    "g": ("g", "gr", "gram", "grams"),
    "kg": ("kg",),
    "lb": ("lb", "lbs", "pound", "pounds"),
    "cup": ("cup", "cups", "c"),
    "tbsp": ("tbsp", "tbs", "tablespoon", "tablespoons"),
    "tsp": ("tsp", "teaspoon", "teaspoons"),
    "ml": ("ml",),
    "l": ("l", "liter", "liters", "litre", "litres"),
    "piece": ("piece", "pieces", "pc", "pcs"),
    "slice": ("slice", "slices"),
    "bowl": ("bowl", "bowls"),
    "can": ("can", "cans"),
    "jar": ("jar", "jars"),
}.items():
    for _w in _words:
        _UNIT_CANON[_w] = _canon


def canonical_unit(word) -> str | None:
    """'Ounces' -> 'oz', 'cups' -> 'cup', 'fl oz' -> 'floz'; unknown -> None."""
    w = " ".join(str(word or "").lower().replace(".", "").split())
    w = w.replace("fl oz", "floz")
    return _UNIT_CANON.get(w)


def parse_amount(text) -> tuple:
    """Required amount > 0 with an optional unit word: '64 oz' -> (64.0, 'oz').

    Returns (value, canonical_unit_or_None). Raises InputError like
    parse_positive. A unit the app doesn't know ("64 blorps") is an error,
    never silently dropped.
    """
    s = "" if text is None else str(text).strip()
    for ch, rep in _VULGAR.items():
        s = s.replace(ch, f" {rep}")
    s = " ".join(s.split())
    s = s.replace("fl. oz", "floz").replace("fl oz", "floz").replace("Fl oz", "floz").replace("FL OZ", "floz")
    unit = None
    m = _TRAILING_WORD.match(s)
    if m:
        unit = canonical_unit(m.group(2))
        if unit is None:
            raise InputError(f"'{text}' — I don't know the unit '{m.group(2)}'. Use oz, g, cup, "
                             f"tbsp, tsp, ml, serving…")
        s = m.group(1).strip()
    return parse_positive(s), unit
