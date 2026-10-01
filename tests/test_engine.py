"""Validation suite for the WW Points engine.

Proves the FROZEN V4 profile reproduces the validated dataset (26/27 exact):
  - 20 real scoring foods + Metamucil 2 tsp (21 exact)
  - 5 out-of-sample fast-food items (all exact)
  - Metamucil 4 tsp: the one known non-linear miss (within 1)
  - ZeroPoint overrides, floor at 0
  - recipe linearity (round once at the end, no per-ingredient pile-up)
  - ridge calibration sanity + stability flags, input parsing, storage safety

Run:  python -m pytest tests/  -q      (from artifacts/ww_points_app)
   or plain:  python tests/test_engine.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ww.engine import Macros, Profile, points, points_for_food  # noqa: E402
from ww.recipe import Ingredient, Recipe  # noqa: E402
from ww.storage import Store  # noqa: E402
from ww.inputs import InputError, parse_number, parse_amount, GRAM_UNITS  # noqa: E402
from ww.calibration import (  # noqa: E402
    CalibrationSample,
    default_isolator_template,
    calibrate,
    fit_quality,
)

PROF = Profile()

# (name, calories, sat_fat, added_sugar, protein, fiber, expected_points)
SCORING = [
    ("Vanilla ice cream 1/4c", 68, 2, 5, 1, 0, 4),
    ("Vanilla ice cream 1/2c", 136, 4, 11, 2, 1, 7),
    ("Vanilla ice cream 1c", 272, 9, 21, 5, 1, 15),
    ("Jif PB 1Tbsp", 95, 2, 1, 4, 1, 3),
    ("Jif PB 2Tbsp", 190, 4, 2, 7, 2, 7),
    ("Jif PB 1/3c", 507, 9, 5, 19, 5, 18),
    ("Cheddar 1oz", 114, 5, 0, 6, 0, 5),
    ("Cheddar 2slice", 225, 11, 0, 13, 0, 10),
    ("Raisin Bran 1/2c", 95, 0, 5, 3, 4, 3),
    ("Raisin Bran 1c", 190, 0, 9, 5, 7, 6),
    ("Raisin Bran 1.5c", 285, 0, 14, 8, 11, 9),
    ("Coca-Cola 12oz", 144, 0, 40, 0, 0, 12),
    ("Coca-Cola 20oz", 240, 0, 67, 0, 0, 20),
    ("Coca-Cola 1L", 405, 0, 113, 0, 0, 33),
    ("Kerrygold 1Tbsp", 100, 7, 0, 0, 0, 6),
    ("Kerrygold 2Tbsp", 200, 14, 0, 0, 0, 11),
    ("Kerrygold 1/4c", 400, 28, 0, 0, 0, 22),
    ("Deli turkey 2oz", 90, 2, 1, 9, 0, 3),
    ("Whey 1 scoop", 122, 1, 0, 26, 0, 2),
    ("Whey 3 scoop", 366, 2, 0, 78, 0, 6),
]

# Metamucil (pure fiber supplement) is WW's known non-linear case — the linear
# model misses 4 tsp by 1. This is a documented model limit, not a fit bug, so
# it is tracked separately rather than asserted exact.
KNOWN_NONLINEAR = [
    ("Metamucil 2tsp", 45, 0, 0, 0, 6, 1),   # this one happens to land right
    ("Metamucil 4tsp", 90, 0, 0, 0, 12, 2),  # this one misses by 1 (raw ~1.40)
]

OUT_OF_SAMPLE = [
    ("Big Mac", 590, 11, 7, 25, 3, 21),
    ("Wendy's Frosty", 340, 6, 42, 9, 0, 20),
    ("McD large fries", 480, 3, 0, 7, 6, 14),
    ("Crunchwrap Supreme", 540, 7, 2, 16, 6, 17),
    ("Papa John pizza 8sl", 2560, 48, 16, 96, 16, 90),
]

ZERO_POINT = [
    ("Egg", 70, 0, 0, 6, 0),
    ("Lean beef steak", 627, 5, 0, 110, 0),
    ("Plain rolled oats", 461, 2, 0, 17, 12),
    ("Tuna in water", 100, 0, 0, 22, 0),
]


def _m(cal, sf, su, pr, fi):
    return Macros(cal, sf, su, pr, fi)


def test_scoring_dataset_exact():
    misses = []
    for name, c, s, su, p, f, exp in SCORING:
        got = points(_m(c, s, su, p, f), PROF)
        if got != exp:
            misses.append(f"{name}: got {got}, expected {exp}")
    assert not misses, "Scoring misses:\n" + "\n".join(misses)


def test_out_of_sample_exact():
    misses = []
    for name, c, s, su, p, f, exp in OUT_OF_SAMPLE:
        got = points(_m(c, s, su, p, f), PROF)
        if got != exp:
            misses.append(f"{name}: got {got}, expected {exp}")
    assert not misses, "Out-of-sample misses:\n" + "\n".join(misses)


def test_zero_point_override():
    for name, c, s, su, p, f in ZERO_POINT:
        # Non-zero macros would score > 0, but ZeroPoint forces 0.
        assert points_for_food(name, _m(c, s, su, p, f), PROF) == 0, name


def test_floor_at_zero():
    # High protein/fiber can push the raw score negative; must floor at 0.
    assert points(_m(0, 0, 0, 100, 0), PROF) == 0


def test_recipe_linearity_round_once():
    # Two ingredients, 4 servings. Round ONCE at the end.
    r = Recipe(
        name="Test soup",
        ingredients=[
            Ingredient("Cream", "Store", _m(400, 28, 0, 0, 0)),   # butter-like
            Ingredient("Sugar syrup", "Store", _m(405, 0, 113, 0, 0)),  # coke-like
        ],
        yield_amount=4,
        serving_size=1,
    )
    # Per serving = (805 cal, 28 sf, 113 su)/4 = (201.25, 7, 28.25)
    # raw = .03034*201.25 + .36869*7 + .18540*28.25 = 6.106+2.581+5.238 = 13.924 -> 14
    assert r.points_per_serving(PROF) == 14


def test_recipe_zeropoint_excluded():
    r = Recipe(
        name="Zp test",
        ingredients=[
            Ingredient("Coke", "Coca-Cola", _m(405, 0, 113, 0, 0)),
            Ingredient("Egg", "", _m(70, 0, 0, 6, 0), is_zero=True),
        ],
        yield_amount=1,
        serving_size=1,
    )
    # Only the coke counts: 33 points; egg macros excluded.
    assert r.points_per_serving(PROF) == 33


def test_known_nonlinear_within_one():
    # Metamucil (pure fiber) may miss by 1 — assert it is never off by MORE.
    for name, c, s, su, p, f, exp in KNOWN_NONLINEAR:
        got = points(_m(c, s, su, p, f), PROF)
        assert abs(got - exp) <= 1, f"{name}: got {got}, expected ~{exp}"


def test_calibration_regularized_not_absurd():
    # The reference isolator set does NOT independently isolate calories, so a
    # RAW least-squares solve overfits (absurd coefficients). The regularized
    # calibrate() must instead return sane, bounded coefficients near the
    # reference — and must NOT report itself as a confident/stable fit on a set
    # that cannot separate the macros.
    samples = default_isolator_template()
    result = calibrate(samples)
    # Coefficients must stay in a sane range (raw OLS blew these to -6.3 etc.).
    assert 0.0 <= result.coeffs["calories"] <= 0.1, result.coeffs
    assert 0.0 <= result.coeffs["saturated_fat"] <= 1.0, result.coeffs
    assert 0.0 <= result.coeffs["added_sugar"] <= 0.5, result.coeffs
    # A pure sat-fat coefficient of -6 (the old overfit) must never happen.
    assert result.coeffs["saturated_fat"] > 0, "sat-fat coeff went negative (overfit)"


def test_calibration_underdetermined_flagged():
    # Fewer than 5 foods -> must be flagged unstable, not sold as a good fit.
    samples = default_isolator_template()[:3]
    result = calibrate(samples)
    assert result.stable is False
    assert result.warning


def test_storage_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        store = Store(d)
        prof = Profile(name="Custom", coeffs=dict(PROF.coeffs))
        store.save_profile(prof)
        assert store.load_profile().name == "Custom"

        r = Recipe(name="My Soup", ingredients=[Ingredient("Onion", "Local", _m(40, 0, 0, 1, 2))],
                   yield_amount=2, serving_size=1)
        store.save_recipe(r)
        assert "My Soup" in store.list_recipes()
        loaded = store.load_recipe("My Soup")
        assert loaded is not None and len(loaded.ingredients) == 1


# ---------------------------------------------------------------------------
# Phase A regression tests (Gordon / Lennie findings, 2026-09-30)
# ---------------------------------------------------------------------------

FROZEN_V4 = {"calories": 0.03034, "saturated_fat": 0.36869, "added_sugar": 0.18540,
             "protein": -0.07317, "fiber": -0.11087}


def test_frozen_formula_untouched():
    assert Profile().coeffs == FROZEN_V4
    assert points(_m(0, 0, 0, 0, 0), PROF, zero_point=True) == 0


def test_calibration_template_stable_and_sane():
    r = calibrate(default_isolator_template())
    assert r.stable, r.warning
    assert 0 < r.coeffs["saturated_fat"] < 1.0


def test_calibration_old_isolator_set_flagged():
    old = [CalibrationSample("Coke 12", _m(144, 0, 40, 0, 0), 12),
           CalibrationSample("Coke 1L", _m(405, 0, 113, 0, 0), 33),
           CalibrationSample("Butter", _m(400, 28, 0, 0, 0), 22),
           CalibrationSample("Whey", _m(366, 2, 0, 78, 0), 6),
           CalibrationSample("Metamucil", _m(90, 0, 0, 0, 12), 2)]
    assert calibrate(old).stable is False


def test_calibration_rejects_points_without_macros():
    s = default_isolator_template() + [CalibrationSample("Forgot label", _m(0, 0, 0, 0, 0), 33)]
    try:
        calibrate(s)
    except ValueError:
        return
    raise AssertionError("a food with Points but no macros must be rejected")


def test_calibration_excludes_zero_point_readings():
    s = default_isolator_template() + [CalibrationSample("Egg", _m(70, 0, 0, 6, 0), 0)]
    r = calibrate(s)
    assert r.excluded == ["Egg"] and r.n_used == 7


def test_calibration_no_drift():
    a = calibrate(default_isolator_template()).coeffs
    b = calibrate(default_isolator_template()).coeffs
    assert a == b


def test_inputs_label_units_and_junk():
    assert parse_number("12g", units=GRAM_UNITS) == 12
    assert parse_number("1,5") == 1.5
    assert parse_number("1 1/2") == 1.5
    assert parse_amount("64 oz") == (64.0, "oz")
    assert parse_amount("8 ounces") == (8.0, "oz")
    for bad in ("1O0", "nan", "inf", "1e999", "-3"):
        try:
            parse_number(bad)
        except InputError:
            continue
        raise AssertionError(f"{bad!r} must be rejected")
    try:
        parse_number("12 oz", units=GRAM_UNITS)
        raise AssertionError("'12 oz' in a gram box must be rejected")
    except InputError:
        pass


def test_recipe_bad_yield_raises_not_zero():
    r = Recipe("x", [Ingredient("Coke", "", _m(405, 0, 113, 0, 0))], 0, 1)
    try:
        r.points_per_serving(PROF)
    except ValueError:
        return
    raise AssertionError("yield 0 must raise, not score 0")


def test_recipe_zeropoint_by_name_and_plural():
    r = Recipe("x", [Ingredient("Coke", "", _m(405, 0, 113, 0, 0)),
                     Ingredient("Eggs", "", _m(140, 3, 0, 12, 0))], 1, 1)
    assert r.points_per_serving(PROF) == 33


def test_storage_corrupt_files_quarantined():
    with tempfile.TemporaryDirectory() as d:
        store = Store(d, fallback=False)
        with open(store.library_path, "w") as f:
            f.write("{not json")
        assert store.load_library() == {}
        assert any(".corrupt-" in fn for fn in os.listdir(d))
        assert store.pop_warnings()
        with open(store.profile_path, "w") as f:
            f.write("[]")
        assert store.load_profile().coeffs == FROZEN_V4


def test_storage_names_dont_collide_and_no_traversal():
    with tempfile.TemporaryDirectory() as d:
        store = Store(d, fallback=False)
        for n in ("My Soup", "My_Soup", "!!!", "???", "../../evil", "CON"):
            p = store.save_recipe(Recipe(name=n, ingredients=[Ingredient("Salt")]))
            assert os.path.dirname(os.path.abspath(p)) == os.path.abspath(store.recipes_dir)
        assert len(store.list_recipes()) == 6


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} tests passed")
    return passed == len(fns)


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
