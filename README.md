# WW Points Calculator

A cross-platform (Windows + Mac) desktop app for the WW **Points** program.
Score single foods, calculate whole recipes, keep a library of your ingredients
and recipes, and calibrate the formula to your own WW profile. Fully offline —
everything is stored in local files next to the app.

---

## Quick start

You need Python 3.9 or newer.

```bash
cd ww_points_app
python3 -m pip install -r requirements.txt     # Mac/Linux
# (on Windows:  py -m pip install -r requirements.txt)
python3 main.py                                # launches the app window
```

The app opens as a native desktop window with four tabs: **Single food**,
**Recipe**, **Calibrate**, and **Help**.

---

## How to use it

### Single food
Score one food from its nutrition label.
1. (Optional) type the food name — if it's on your ZeroPoint list, it auto-scores 0.
2. Enter the macros: **Calories, Saturated fat (g), Added sugar (g), Protein (g), Fiber (g)**.
   You can include units (`590 cal`, `11 g`) — the app understands them.
3. Flip the **ZeroPoint** toggle for any food that logs as 0 regardless of macros.
4. The points update as you type; the small line underneath shows the raw score
   so you can see how close it was to rounding up or down.

> **Use added sugar, not total sugar**, and **fiber** — this is the current
> *Points* program, not the older SmartPoints model.

### Recipe
Score a whole recipe and get points **per serving**.
1. Name the recipe, then set **Total yield** and **Serving size** in the same unit
   (e.g. yield `64 oz`, serving `8 oz` → 8 servings). The app does **not** convert
   between units — keep yield and serving in the same unit.
2. Add each ingredient with its **name + brand** and the macros for the **total
   amount that goes in the pot** (not per serving).
3. Tick **ZP** for any ZeroPoint ingredient — it contributes 0 points.
4. **Calculate** shows points per serving (rounded once, at the end — no
   per-ingredient rounding pile-up) plus an auto **shopping list**.
5. **Save recipe** writes it to disk. Saving over an existing name asks before
   replacing. **Open** reloads a saved recipe with every field and the shopping
   list restored.

**Ingredient library:** ingredients you save are remembered. Start typing an
ingredient name in a recipe row and pick from the suggestions to auto-fill its
macros.

### Calibrate (tune it to your own WW profile)
Every WW member's profile differs. Calibrate the five coefficients to yours by
logging **isolator foods** — each high in ONE macro and near-zero in the others —
plus a few mixed real foods:

- **Coca-Cola** → added sugar (and, with calories, the calorie term)
- **Butter** → saturated fat
- **Whey in water** → protein
- **Raisin Bran** (a *real* fiber food) → fiber
- **Peanut butter / ice cream / cheddar** → mixed foods that tie it together

> Don't calibrate fiber with a pure fiber supplement like **Metamucil** — WW's
> fiber handling is non-linear for pure supplements, so it throws the fit off.

Steps: the tab is pre-filled with reference foods — replace the **Points** with
your own WW app's readings, add/remove foods, then **Fit coefficients**. Review
the result and the stability check, and **Apply** to save it to your profile.
**Reset to reference** restores the built-in profile anytime.

The app warns you when your foods can't cleanly separate the five macros (too few,
or too similar) — a low error score on such a set is an *overfit*, not a good fit,
so it leans on the reference profile and tells you.

### Help
The formula and the calibration method, in-app.

---

## The formula

```
Points = round( 0.03034·cal + 0.36869·sat_fat + 0.18540·added_sugar
                − 0.07317·protein − 0.11087·fiber )        (floored at 0)
```

Validated to **26/27 exact** across the test set — all real foods and all five
out-of-sample fast-food items (Big Mac, Frosty, large fries, Crunchwrap, a whole
Papa John's pizza) score exactly. The single miss is Metamucil 4 tsp (pure fiber,
the known non-linear case), off by 1.

```bash
python3 tests/test_engine.py      # run the validation + regression suite (21 tests)
```

---

## Where your data lives

Everything is saved as plain JSON next to the app, in a `data/` folder:

- `data/profile.json` — your active coefficients + ZeroPoint list
- `data/library.json` — your reusable ingredient library
- `data/recipes/*.json` — one file per saved recipe

No account, no server, no network — it all stays on your machine. If the app
folder isn't writable, it falls back to a per-user data directory and tells you
where.

---

## Project layout

```
ww_points_app/
├── main.py              # the app (UI: single food, recipe, calibrate, help)
├── requirements.txt     # flet + numpy
├── README.md
├── ww/
│   ├── engine.py        # the scoring formula + Profile (coefficients, ZeroPoint)
│   ├── recipe.py        # Ingredient / Recipe — linear sum, round once
│   ├── calibration.py   # ridge-regularized self-calibration
│   ├── inputs.py        # tolerant number/unit parsing
│   └── storage.py       # local JSON persistence (atomic, crash-safe)
└── tests/
    └── test_engine.py   # validation + regression suite
```

---

## Troubleshooting

- **`command not found: python`** on Mac → use `python3` (and `python3 -m pip`).
- **`No module named flet`** after installing → you likely have more than one
  Python; install into the same one you run with:
  `python3 -m pip install -r requirements.txt`.
- **Window doesn't appear** → make sure `pip install` finished without errors;
  Flet downloads a runtime on first install.

---

*Offline, local, yours. Built with the WW Points model reverse-engineered and
validated against real food data.*
