"""WW Points Calculator — self-calibration.

Any WW member can tune the five coefficients to their own personalized
profile by logging foods they know the Points for. The best foods are
"isolators" — high in ONE macro, near-zero in the others — plus a few mixed
real foods so every macro is pinned down by more than one data point:

    - Coca-Cola      -> added sugar (and, with calories, the calorie term)
    - Butter         -> saturated fat
    - Whey in water  -> protein
    - Raisin Bran    -> fiber (a REAL fiber food — pure fiber supplements
                        such as Metamucil are WW's known non-linear case and
                        must NOT be used to calibrate fiber)
    - Peanut butter, ice cream, cheddar -> mixed foods that tie it together

WHY THIS IS NOT A PLAIN LEAST-SQUARES FIT
------------------------------------------
Five isolator foods give five equations for five unknowns. A plain solve then
hits the data EXACTLY (RMSE ~ 0) — but if the foods don't each isolate a
DIFFERENT macro, the "perfect" solution is garbage (raw OLS on the old 5-food
template gave saturated_fat = -6.36). A near-zero RMSE on a barely-determined
system is the overfit tell, NOT a success.

So calibration is RIDGE-REGULARIZED toward a FIXED reference (the frozen V4
coefficients by default — never the user's current, possibly already
calibrated, profile: re-anchoring on the last result would let repeated
Fit -> Apply clicks walk the fit back to the overfit OLS solution).

STABILITY = "would the data alone pin the coefficients down?"
--------------------------------------------------------------
WW shows whole-number Points, so every observation carries up to +/-0.5 of
rounding noise (uniform, std 0.5/sqrt(3) ~= 0.289). We propagate that noise
through the UNREGULARIZED solve and ask how much each coefficient would wobble
relative to its reference size. If any coefficient wobbles by more than
REL_SENSITIVITY_LIMIT (50%), the data can't identify it and the fit is flagged
unstable (the ridge result then mostly reflects the reference). This catches
the old 5-isolator template (wobble 900%–3250%) that the condition number
alone (2620 < 1e4) passed as "stable".

Censored data: an observation of 0 Points is either floored (true raw score
negative and unknown) or a ZeroPoint food (macros irrelevant). Neither is a
valid linear data point, so observations <= 0 are EXCLUDED and reported.

NOTE: this module never changes the frozen V4 engine. It produces a separate,
user-owned coefficient set that the user may choose to apply to their profile.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from typing import Dict, List

from .engine import Macros, Profile, MACRO_KEYS, V4_ASHLEY


@dataclass
class CalibrationSample:
    """One observed data point for calibration."""

    label: str
    macros: Macros
    observed_points: float


@dataclass
class CalibrationResult:
    """Outcome of a calibration fit."""

    coeffs: Dict[str, float]
    rmse: float
    condition_number: float
    stable: bool
    warning: str = ""
    exact_matches: int = 0          # used samples the fit scores exactly (after rounding)
    n_used: int = 0                 # samples that entered the fit
    excluded: List[str] = field(default_factory=list)   # labels excluded (<= 0 pts)
    sensitivity: Dict[str, float] = field(default_factory=dict)  # rel. wobble per coeff


def _matrix_row(m: Macros) -> List[float]:
    return [m.calories, m.saturated_fat, m.added_sugar, m.protein, m.fiber]


# Condition number above this = the food set is too collinear to trust.
CONDITION_WARN_THRESHOLD = 1e4
# Max allowed 1-sigma relative wobble of any unregularized coefficient under
# +/-0.5 rounding noise. Template of 7 real foods ~= 0.44; old 5-isolator ~= 32.
REL_SENSITIVITY_LIMIT = 0.5
# Std of uniform rounding noise on a whole-number Points reading.
ROUNDING_NOISE_STD = 0.5 / math.sqrt(3.0)
MIN_SAMPLES = 5
# Largest macro / Points value accepted (matches ww.inputs.MAX_INPUT); stops
# overflow to inf inside the solve.
MAX_VALUE = 1_000_000.0


def _validate_sample(s: CalibrationSample) -> None:
    for k, v in zip(MACRO_KEYS, _matrix_row(s.macros)):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            raise ValueError(f"'{s.label}': {k} must be a finite number.")
        if v < 0:
            raise ValueError(f"'{s.label}': {k} can't be negative.")
        if v > MAX_VALUE:
            raise ValueError(f"'{s.label}': {k} is too large (max {MAX_VALUE:,.0f}).")
    op = s.observed_points
    if not isinstance(op, (int, float)) or isinstance(op, bool) or not math.isfinite(op):
        raise ValueError(f"'{s.label}': Points must be a finite number.")
    if abs(op) > MAX_VALUE:
        raise ValueError(f"'{s.label}': Points is too large (max {MAX_VALUE:,.0f}).")
    if float(op) != int(op):
        # WW only shows whole numbers, and the +/-0.5 rounding-noise model
        # behind the stability test assumes integer readings.
        raise ValueError(f"'{s.label}': WW Points are whole numbers — type exactly what "
                         f"your WW app shows (got {op:g}).")
    if op > 0 and all(v == 0 for v in _matrix_row(s.macros)):
        # No coefficients can turn all-zero macros into >0 Points: the row is a
        # forgotten-label typo. It would drop out of the solve silently while
        # still counting toward n_used / the 5-food minimum.
        raise ValueError(f"'{s.label}': has {op:g} WW Points but no label numbers — type its "
                         f"calories, sat fat, sugar, protein and fiber (or remove the row).")


def _predict(coeffs: Dict[str, float], m: Macros) -> float:
    return sum(coeffs[k] * v for k, v in zip(MACRO_KEYS, _matrix_row(m)))


def calibrate(
    samples: List[CalibrationSample],
    prior: Dict[str, float] | None = None,
    ridge: float = 1.0,
    *,
    base: Profile | None = None,
) -> CalibrationResult:
    """Ridge-regularized calibration toward a FIXED reference.

    prior: reference coefficients to regularize toward. Default: frozen V4.
        Do NOT pass the user's current calibrated profile (see module doc).
    ridge: >= 0. Strength of the pull toward the prior, in scaled units
        (each column divided by its max |value|, so 1.0 is comparable to one
        full-size food observation per macro). 0 = plain least squares
        (minimum-norm), which will overfit a barely-determined set.
    base: DEPRECATED — old API that used base.coeffs as the prior. Still
        honoured (with a DeprecationWarning) for external callers; the app no
        longer passes it.

    Raises ValueError for no/invalid samples or invalid ridge, ImportError if
    numpy is missing.
    """
    if not samples:
        raise ValueError("No calibration samples provided.")
    if not isinstance(ridge, (int, float)) or isinstance(ridge, bool) \
            or not math.isfinite(ridge) or ridge < 0:
        raise ValueError("ridge must be a finite number >= 0.")
    if prior is None and base is not None:
        warnings.warn("calibrate(base=...) is deprecated; pass prior=... "
                      "(a fixed reference), not the current profile.",
                      DeprecationWarning, stacklevel=2)
        prior = base.coeffs
    prior = dict(V4_ASHLEY) if prior is None else dict(prior)
    for k in MACRO_KEYS:
        v = prior.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            raise ValueError(f"prior coefficient '{k}' missing or not finite.")

    for s in samples:
        _validate_sample(s)
    used = [s for s in samples if s.observed_points > 0]
    excluded = [s.label for s in samples if s.observed_points <= 0]
    if not used:
        raise ValueError(
            "Every food has 0 Points. 0-Point readings can't be used to calibrate "
            "(they're floored or ZeroPoint foods) — add foods that score 1 or more."
        )

    import numpy as np

    prior_vec = np.array([prior[k] for k in MACRO_KEYS], dtype=float)
    A = np.array([_matrix_row(s.macros) for s in used], dtype=float)
    b = np.array([float(s.observed_points) for s in used], dtype=float)
    n = A.shape[1]

    # Column-scale so the ridge penalty is comparable across macros of very
    # different magnitudes (calories ~100s vs fiber ~10s). An all-zero column
    # gets scale 1 (its coefficient is unidentified and stays at the prior).
    colmax = np.abs(A).max(axis=0)
    scale = np.where(colmax > 0, colmax, 1.0)
    A_s = A / scale
    prior_s = prior_vec * scale  # prior in scaled space

    # Ridge / Tikhonov: minimize ||A_s x - b||^2 + ridge*||x - prior_s||^2
    if ridge > 0:
        lhs = A_s.T @ A_s + ridge * np.eye(n)
        rhs = A_s.T @ b + ridge * prior_s
        x_s = np.linalg.solve(lhs, rhs)   # lhs is SPD for ridge > 0
    else:
        # Plain least squares on the DEVIATION from the prior: the min-norm
        # solution then leaves any unidentified direction at the prior
        # (not at 0, which a min-norm solve of the raw system would give).
        x_s = prior_s + np.linalg.lstsq(A_s, b - A_s @ prior_s, rcond=None)[0]
    coeffs_arr = x_s / scale  # back to real space
    coeffs = {k: float(v) for k, v in zip(MACRO_KEYS, coeffs_arr)}

    # ---- diagnostics -----------------------------------------------------
    rank = int(np.linalg.matrix_rank(A_s))
    try:
        cond = float(np.linalg.cond(A_s))
    except np.linalg.LinAlgError:
        cond = float("inf")
    if not math.isfinite(cond):
        cond = float("inf")

    sensitivity: Dict[str, float] = {}
    if rank < n:
        sensitivity = {k: float("inf") for k in MACRO_KEYS}
    else:
        cov = (ROUNDING_NOISE_STD ** 2) * np.linalg.inv(A_s.T @ A_s)
        sd = np.sqrt(np.clip(np.diag(cov), 0.0, None))
        denom = np.maximum(np.abs(prior_s), 1e-12)
        sensitivity = {k: float(v) for k, v in zip(MACRO_KEYS, sd / denom)}
    worst_key = max(sensitivity, key=lambda k: sensitivity[k])
    worst = sensitivity[worst_key]

    rmse = fit_quality(used, coeffs)
    exact = sum(1 for s in used if max(0, round(_predict(coeffs, s.macros))) == s.observed_points)

    stable = True
    warning = ""
    if len(used) < MIN_SAMPLES:
        stable = False
        warning = (
            f"Only {len(used)} usable foods — need {MIN_SAMPLES}+, together covering all "
            f"five macros, for a trustworthy fit. Result leans on the reference profile."
        )
    elif rank < n or cond > CONDITION_WARN_THRESHOLD:
        stable = False
        warning = (
            "Your foods are too similar to separate the five macros (or one macro "
            "never appears). Add foods that are high in ONE macro and near-zero in "
            "the rest, for every macro. Result leans on the reference profile."
        )
    elif worst > REL_SENSITIVITY_LIMIT:
        stable = False
        warning = (
            f"Your foods can't pin down the {worst_key.replace('_', ' ')} coefficient: "
            f"WW's whole-number Points alone could swing it by ~{worst:.0%}. Add more "
            f"foods with plenty of {worst_key.replace('_', ' ')} (and some mixed foods). "
            f"Result leans on the reference profile."
        )
    if excluded:
        note = (f" Excluded {len(excluded)} food(s) logged at 0 Points "
                f"(floored/ZeroPoint readings can't calibrate): {', '.join(excluded)}.")
        warning = (warning + note).strip()

    return CalibrationResult(coeffs=coeffs, rmse=rmse, condition_number=cond,
                             stable=stable, warning=warning, exact_matches=exact,
                             n_used=len(used), excluded=excluded,
                             sensitivity=sensitivity)


# Backwards-compatible thin wrapper (returns just the coeff dict).
def calibrate_least_squares(
    samples: List[CalibrationSample],
    base: Profile | None = None,
) -> Dict[str, float]:
    """Deprecated: use calibrate(). Kept for callers that want only coeffs.

    Ridge-regularized toward the fixed V4 reference (the `base` argument is
    ignored for the prior on purpose — see module doc on re-anchoring drift).
    """
    return calibrate(samples).coeffs


def default_isolator_template() -> List[CalibrationSample]:
    """The recommended starting set (7 real foods, no supplements).

    Pre-filled with the readings behind the validated V4 profile so a new user
    sees the method and replaces the Points with their own program's readings.
    Chosen so the set is STABLE under the noise test (max wobble ~44%):
    the old 5-isolator set (two Cokes, butter, whey, Metamucil) could not
    separate calories from sugar and leaned on Metamucil — the one food the
    linear model is documented NOT to fit.
    """
    return [
        CalibrationSample("Coca-Cola 1 L", Macros(405, 0, 113, 0, 0), 33),
        CalibrationSample("Kerrygold butter 1/4 cup", Macros(400, 28, 0, 0, 0), 22),
        CalibrationSample("Whey (in water) 3 scoop", Macros(366, 2, 0, 78, 0), 6),
        CalibrationSample("Raisin Bran 1.5 cup", Macros(285, 0, 14, 8, 11), 9),
        CalibrationSample("Jif peanut butter 1/3 cup", Macros(507, 9, 5, 19, 5), 18),
        CalibrationSample("Vanilla ice cream 1 cup", Macros(272, 9, 21, 5, 1), 15),
        CalibrationSample("Cheddar 2 slices", Macros(225, 11, 0, 13, 0), 10),
    ]


def is_template_unchanged(samples: List[CalibrationSample]) -> bool:
    """True if the NUMBERS are exactly the pre-filled reference readings.

    Labels and row order are ignored: renaming "Coca-Cola 1 L" to "Coke"
    doesn't turn the reference readings into the user's own data.
    """
    def key(s: CalibrationSample):
        return tuple(float(v) for v in _matrix_row(s.macros)) + (float(s.observed_points),)

    t = default_isolator_template()
    if len(samples) != len(t):
        return False
    return sorted(key(s) for s in samples) == sorted(key(s) for s in t)


def fit_quality(samples: List[CalibrationSample], coeffs: Dict[str, float]) -> float:
    """Root-mean-square error of a coefficient set over the samples."""
    if not samples:
        return 0.0
    total = 0.0
    for s in samples:
        total += (_predict(coeffs, s.macros) - s.observed_points) ** 2
    return (total / len(samples)) ** 0.5
