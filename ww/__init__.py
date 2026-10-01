"""WW Points Calculator — package init."""

from .engine import (
    Macros,
    Profile,
    MACRO_KEYS,
    V3_ASHLEY,
    V4_ASHLEY,
    points,
    raw_points,
    points_for_food,
)
from .recipe import Ingredient, Recipe
from .storage import Store
from .calibration import (
    CalibrationSample,
    CalibrationResult,
    calibrate,
    calibrate_least_squares,
    default_isolator_template,
    fit_quality,
)

__all__ = [
    "Macros",
    "Profile",
    "MACRO_KEYS",
    "V3_ASHLEY",
    "V4_ASHLEY",
    "points",
    "raw_points",
    "points_for_food",
    "Ingredient",
    "Recipe",
    "Store",
    "CalibrationSample",
    "CalibrationResult",
    "calibrate",
    "calibrate_least_squares",
    "default_isolator_template",
    "fit_quality",
]

__version__ = "1.0.0"
