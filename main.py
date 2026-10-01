"""WW Points Calculator — Flet desktop app (Windows + Mac).

A cross-platform GUI for the WW "Points" program:
  - Single-food tab: enter macros + ZeroPoint toggle -> points
  - Recipe tab: N ingredients (name+brand) + yield/serving -> points/serving,
    with an auto shopping list; save & reopen recipes; ingredient names
    autocomplete from your saved ingredient library
  - Calibrate tab: enter foods you know the Points for -> re-fit your own
    coefficients (optional; the reference V4 profile is the default)
  - Help tab: how the model + calibration work

Storage is local JSON (fully offline) in a `data/` folder next to this file,
or in your per-user app-data folder if that one isn't writable.

Run:
    pip install -r requirements.txt
    python main.py

Tested API window: Flet 0.25 – 0.28 (see requirements.txt).
"""

from __future__ import annotations

import os
import flet as ft

from ww.engine import Macros, Profile, points, raw_points, MACRO_KEYS
from ww.inputs import (InputError, parse_number, parse_amount, canonical_unit,
                       GRAM_UNITS, CALORIE_UNITS, POINT_UNITS)
from ww.recipe import Ingredient, Recipe, zero_point_match, zero_point_near_miss
from ww.storage import Store, RecipeExistsError, normalize_recipe_name
from ww.calibration import (
    CalibrationSample,
    calibrate,
    default_isolator_template,
    is_template_unchanged,
)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

MACRO_FIELDS = [
    ("calories", "Calories"),
    ("saturated_fat", "Saturated fat (g)"),
    ("added_sugar", "Added sugar (g)"),
    ("protein", "Protein (g)"),
    ("fiber", "Fiber (g)"),
]
# Short labels for the compact recipe / calibrate rows, same order as MACRO_KEYS.
SHORT_LABELS = {"calories": "Cal", "saturated_fat": "Sat fat g", "added_sugar": "Add. sugar g",
                "protein": "Protein g", "fiber": "Fiber g"}
# Unit words each macro box accepts straight off a label ("12g", "150 cal").
MACRO_UNITS = {k: (CALORIE_UNITS if k == "calories" else GRAM_UNITS) for k in MACRO_KEYS}


def _pts(n: int) -> str:
    """'1 pt', '0 pts', '14 pts'."""
    return f"{n} pt" if n == 1 else f"{n} pts"


def _fmt(v: float) -> str:
    """Number for a text box: 12.0 -> '12', 12.5 -> '12.5'."""
    return f"{v:g}"


def _read(field: ft.TextField, parser=parse_number, **kw):
    """Parse a box; show/clear its error text. Returns (value, ok)."""
    try:
        v = parser(field.value, **kw)
        field.error_text = None
        return v, True
    except InputError as e:
        field.error_text = str(e)
        return None, False


class WWApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.store = Store(DATA_DIR)
        self.profile: Profile = self.store.load_profile()
        self.library = self.store.load_library()
        self._pending_result = None  # last calibration fit, cleared on any edit
        self._open_recipe_name = None  # saved recipe currently loaded in the editor
        self._recalcs = []  # tab re-calculators, run when the profile changes

        page.title = "WW Points Calculator"
        try:
            page.window.width = 900
            page.window.height = 760
        except AttributeError:  # older Flet
            page.window_width = 900
            page.window_height = 760
        page.theme_mode = ft.ThemeMode.LIGHT
        page.padding = 0

        self.profile_banner = ft.Text(
            self._profile_label(), size=12, italic=True, color=ft.Colors.BLUE_GREY_600
        )
        self.warning_banner = ft.Text("", size=12, color=ft.Colors.ORANGE_900, visible=False)

        self.tabs = ft.Tabs(
            selected_index=0,
            animation_duration=200,
            expand=True,
            tabs=[
                ft.Tab(text="Single food", icon=ft.Icons.RESTAURANT, content=self._single_tab()),
                ft.Tab(text="Recipe", icon=ft.Icons.SOUP_KITCHEN, content=self._recipe_tab()),
                ft.Tab(text="Calibrate", icon=ft.Icons.TUNE, content=self._calibrate_tab()),
                ft.Tab(text="Help", icon=ft.Icons.HELP_OUTLINE, content=self._help_tab()),
            ],
        )

        page.add(
            ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Text("WW Points Calculator", size=20, weight=ft.FontWeight.BOLD),
                                self.profile_banner,
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                        self.warning_banner,
                        self.tabs,
                    ],
                    expand=True,
                ),
                padding=16,
                expand=True,
            )
        )
        self._show_store_warnings()

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------
    def _profile_label(self) -> str:
        return f"Profile: {self.profile.name}"

    def _refresh_profile_banner(self):
        self.profile_banner.value = self._profile_label()
        self.page.update()

    def _profile_changed(self):
        """Profile was applied/reset: refresh the banner AND every shown result,
        so no tab keeps displaying a number computed with the old profile."""
        self.profile_banner.value = self._profile_label()
        for fn in self._recalcs:
            fn()
        self._show_store_warnings()
        self.page.update()

    def _show_store_warnings(self):
        w = self.store.pop_warnings()
        if w:
            existing = [self.warning_banner.value] if self.warning_banner.value else []
            self.warning_banner.value = "\n".join(existing + ["⚠ " + x for x in w])
            self.warning_banner.visible = True
            self.page.update()

    def _open(self, ctrl):
        """Show a SnackBar/AlertDialog on any supported Flet version."""
        if hasattr(self.page, "open"):
            self.page.open(ctrl)
        else:  # pragma: no cover - very old Flet
            if isinstance(ctrl, ft.SnackBar):
                self.page.snack_bar = ctrl
            else:
                self.page.dialog = ctrl
            ctrl.open = True
            self.page.update()

    def _close(self, ctrl):
        if hasattr(self.page, "close"):
            self.page.close(ctrl)
        else:  # pragma: no cover
            ctrl.open = False
            self.page.update()

    def _snack(self, msg: str):
        self._open(ft.SnackBar(ft.Text(msg)))

    def _confirm(self, title: str, body: str, ok_label: str, on_ok):
        dlg = ft.AlertDialog(modal=True, title=ft.Text(title), content=ft.Text(body))

        def ok(_):
            self._close(dlg)
            on_ok()

        dlg.actions = [ft.TextButton("Cancel", on_click=lambda e: self._close(dlg)),
                       ft.ElevatedButton(ok_label, on_click=ok)]
        self._open(dlg)

    # ------------------------------------------------------------------
    # Single-food tab
    # ------------------------------------------------------------------
    def _single_tab(self) -> ft.Container:
        self.sf_name = ft.TextField(label="Food name (optional)", width=340)
        self.sf_inputs = {
            key: ft.TextField(label=label, width=180, keyboard_type=ft.KeyboardType.NUMBER,
                              hint_text="0")
            for key, label in MACRO_FIELDS
        }
        self.sf_zero = ft.Switch(label="ZeroPoint food (log as 0)", value=False)
        self.sf_result = ft.Text("", size=42, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_700)
        self.sf_detail = ft.Text("", size=12, color=ft.Colors.BLUE_GREY_600)

        def calc(_=None):
            vals, ok = {}, True
            for k, f in self.sf_inputs.items():
                v, good = _read(f, units=MACRO_UNITS[k])
                vals[k] = v
                ok = ok and good
            if not ok:
                self.sf_result.value = "—"
                self.sf_detail.value = "Fix the box(es) marked in red — no points shown until every box is a number."
                self.page.update()
                return
            m = Macros(**vals)
            # Auto-detect ZeroPoint by name against the profile list — the SAME
            # rule as recipe rows (exact name or simple plural, any case). The
            # toggle is an OR on top (force ZeroPoint for a food not on the list).
            name_is_zero = zero_point_match(self.profile, self.sf_name.value)
            is_zero = bool(self.sf_zero.value) or name_is_zero
            if not is_zero and not any((f.value or "").strip() for f in self.sf_inputs.values()):
                # Nothing typed yet: don't show a confident "0 pts".
                self.sf_result.value = ""
                self.sf_detail.value = "Type the numbers from the nutrition label to see the points."
                self.page.update()
                return
            pts = points(m, self.profile, zero_point=is_zero)
            raw = 0.0 if is_zero else max(0.0, raw_points(m, self.profile))
            self.sf_result.value = _pts(pts)
            if is_zero:
                why = "on your ZeroPoint list" if name_is_zero and not self.sf_zero.value else "toggled ZeroPoint"
                self.sf_detail.value = f"ZeroPoint ({why}) — logs as 0"
            else:
                self.sf_detail.value = f"raw score {raw:.2f} (rounds to {pts})"
                near = zero_point_near_miss(self.profile, self.sf_name.value)
                if near:
                    self.sf_detail.value += (f" · '{near}' is on your ZeroPoint list — if this is just "
                                             f"{near}, switch on ZeroPoint.")
            self.page.update()

        for f in self.sf_inputs.values():
            f.on_change = calc
        self.sf_zero.on_change = calc
        self.sf_name.on_change = calc
        self._recalcs.append(lambda: calc() if self.sf_result.value else None)

        def clear(_=None):
            self.sf_name.value = ""
            for f in self.sf_inputs.values():
                f.value = ""
                f.error_text = None
            self.sf_zero.value = False
            self.sf_result.value = ""
            self.sf_detail.value = ""
            self.page.update()

        grid = ft.Row(
            [self.sf_inputs["calories"], self.sf_inputs["saturated_fat"], self.sf_inputs["added_sugar"]],
            wrap=True,
        )
        grid2 = ft.Row([self.sf_inputs["protein"], self.sf_inputs["fiber"]], wrap=True)

        return ft.Container(
            ft.Column(
                [
                    self.sf_name,
                    ft.Text("Type the numbers from the nutrition label. Leave a box empty for 0.",
                            size=12, italic=True),
                    grid,
                    grid2,
                    self.sf_zero,
                    ft.Row([
                        ft.ElevatedButton("Calculate", icon=ft.Icons.CALCULATE, on_click=calc),
                        ft.TextButton("Clear", icon=ft.Icons.CLEAR, on_click=clear),
                    ]),
                    ft.Divider(),
                    self.sf_result,
                    self.sf_detail,
                ],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
            ),
            padding=20,
        )

    # ------------------------------------------------------------------
    # Recipe tab
    # ------------------------------------------------------------------
    def _recipe_tab(self) -> ft.Container:
        self.rc_name = ft.TextField(label="Recipe name", width=300)
        self.rc_yield = ft.TextField(label="Total yield", width=140, value="1")
        self.rc_serving = ft.TextField(label="Serving size", width=140, value="1")
        self.rc_unit = ft.TextField(label="Unit (oz, g, cup...)", width=160, value="serving")
        self.rc_rows = ft.Column(spacing=6)
        self.rc_result = ft.Text("", size=28, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_700)
        self.rc_note = ft.Text("", size=12, color=ft.Colors.BLUE_GREY_600)
        self.rc_shopping = ft.Column(spacing=2)
        self.rc_saved = ft.Dropdown(label="Saved recipes", width=300, options=[])
        self._refresh_saved_recipes()

        def remove_row(block):
            if block in self.rc_rows.controls:
                self.rc_rows.controls.remove(block)
            if not self.rc_rows.controls:
                add_row()
            calc()

        def fill_from_library(d, ing: Ingredient):
            d["name"].value = ing.name
            d["brand"].value = ing.brand
            d["name"].error_text = None
            d["brand"].error_text = None
            for k in MACRO_KEYS:
                d[k].value = _fmt(getattr(ing.macros, k))
                d[k].error_text = None
            d["zero"].value = bool(ing.is_zero)
            # Library macros are the amounts from the LAST recipe this ingredient
            # was saved in — say so, so a different amount gets corrected.
            d["sugg"].controls = [ft.Text(
                "Filled from your ingredient library — these are the amounts you used last time. "
                "Change them if this recipe uses a different amount.",
                size=11, italic=True, color=ft.Colors.BLUE_GREY_700)]
            d["sugg"].visible = True
            calc()

        def show_suggestions(d):
            q = d["name"].value or ""
            matches = self.store.search_library(q, limit=6, library=self.library)
            # Don't nag once the row already IS that library item (same brand,
            # name AND numbers). Typing the exact name of a saved ingredient into
            # an empty row must still offer it — that's the commonest use.
            cur = Store.library_key(d["brand"].value, d["name"].value)

            def already_filled(m):
                if Store.library_key(m.brand, m.name) != cur:
                    return False
                for k in MACRO_KEYS:
                    try:
                        v = parse_number(d[k].value, units=MACRO_UNITS[k])
                    except InputError:
                        return False
                    if abs(v - getattr(m.macros, k)) > 1e-9:
                        return False
                return True

            matches = [m for m in matches if not already_filled(m)]
            d["sugg"].controls = [ft.Text("From your ingredient library — click to fill:", size=11, italic=True)] + [
                ft.TextButton(
                    f"{(m.brand + ' ') if m.brand else ''}{m.name} — {_fmt(m.macros.calories)} cal"
                    + (" (ZeroPoint)" if m.is_zero else ""),
                    on_click=lambda e, m=m: fill_from_library(d, m),
                )
                for m in matches
            ] if matches else []
            d["sugg"].visible = bool(matches)

        def add_row(ing: Ingredient | None = None):
            d = {
                "name": ft.TextField(label="Ingredient", width=160, value=(ing.name if ing else "")),
                "brand": ft.TextField(label="Brand", width=120, value=(ing.brand if ing else "")),
            }
            for k in MACRO_KEYS:
                d[k] = ft.TextField(label=SHORT_LABELS[k], width=90,
                                    value=(_fmt(getattr(ing.macros, k)) if ing else ""), hint_text="0")
            d["zero"] = ft.Checkbox(label="ZeroPoint", value=(ing.is_zero if ing else False))
            d["sugg"] = ft.Column(spacing=0, visible=False)

            def on_name(e, d=d):
                show_suggestions(d)
                calc()

            d["name"].on_change = on_name
            d["brand"].on_change = calc
            for k in MACRO_KEYS:
                d[k].on_change = calc
            d["zero"].on_change = calc

            block = ft.Column(spacing=0)
            remove = ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip="Remove this ingredient",
                                   on_click=lambda e, b=block: remove_row(b))
            block.controls = [
                ft.Row([d["name"], d["brand"]] + [d[k] for k in MACRO_KEYS] + [d["zero"], remove], wrap=True),
                d["sugg"],
            ]
            block.data = d
            self.rc_rows.controls.append(block)
            self.page.update()

        def collect_recipe():
            """Returns (Recipe, ok). ok=False -> boxes are marked; don't trust the recipe."""
            ok = True
            ings = []
            for block in self.rc_rows.controls:
                d = block.data
                vals = {}
                for k in MACRO_KEYS:
                    v, good = _read(d[k], units=MACRO_UNITS[k])
                    vals[k] = v
                    ok = ok and good
                name = (d["name"].value or "").strip()
                has_numbers = any((d[k].value or "").strip() not in ("", "0") for k in MACRO_KEYS)
                if not name:
                    if has_numbers or (d["brand"].value or "").strip():
                        d["name"].error_text = "Give this ingredient a name (or remove the row)."
                        ok = False
                    else:
                        d["name"].error_text = None
                    continue
                d["name"].error_text = None
                if all(v is not None for v in vals.values()):
                    ings.append(Ingredient(name=name, brand=(d["brand"].value or "").strip(),
                                           macros=Macros(**vals), is_zero=bool(d["zero"].value)))
            ya, gy = _read(self.rc_yield, parse_amount)
            sa, gs = _read(self.rc_serving, parse_amount)
            unit_box = (self.rc_unit.value or "").strip()
            unit = unit_box or "serving"
            if gy and gs:
                # Units typed in the boxes ("64 oz", "1 cup") must agree with each
                # other and with the Unit box — the app never converts units, and
                # silently dividing 64 by 1 would claim 64 servings.
                box_u = canonical_unit(unit_box)
                typed = {u for u in (ya[1], sa[1]) if u}
                every = typed | ({box_u} if box_u and box_u != "serving" else set())
                if len(every) > 1:
                    self.rc_serving.error_text = (
                        f"Yield and serving size must be in the same unit (you have "
                        f"{' and '.join(sorted(every))}). The app doesn't convert units.")
                    gs = False
                elif typed and (box_u in (None, "serving") or not unit_box) and len(typed) == 1:
                    unit = next(iter(typed))
            ok = ok and gy and gs
            r = Recipe(
                name=" ".join((self.rc_name.value or "").split()),
                ingredients=ings,
                yield_amount=ya[0] if gy else 1.0,
                serving_size=sa[0] if gs else 1.0,
                unit=unit,
            )
            return r, ok

        def calc(_=None):
            r, ok = collect_recipe()
            if not ok:
                self.rc_result.value = "—"
                self.rc_note.value = "Fix the box(es) marked in red — no points shown until everything is valid."
                self.rc_shopping.controls = []
                self.page.update()
                return
            if not r.ingredients:
                self.rc_result.value = ""
                self.rc_note.value = "Add at least one ingredient (give it a name)."
                self.rc_shopping.controls = []
                self.page.update()
                return
            pts = r.points_per_serving(self.profile)
            raw = r.raw_points_per_serving(self.profile)
            n = r.servings()
            self.rc_result.value = f"{_pts(pts)} per serving ({_fmt(r.serving_size)} {r.unit})"
            zp = [i.name for i in r.ingredients if r.is_zero_ingredient(i, self.profile)]
            note = f"raw {raw:.2f} per serving · {n:g} servings in the recipe"
            if zp:
                note += f" · counted as ZeroPoint (0): {', '.join(zp)}"
            if r.serving_size > r.yield_amount:
                note += " · ⚠ serving size is bigger than the whole recipe — check yield vs serving"
            self.rc_note.value = note
            self.rc_shopping.controls = [ft.Text("Shopping list:", weight=ft.FontWeight.BOLD)] + [
                ft.Text(f"  • {item}") for item in r.shopping_list()
            ]
            self.page.update()

        def do_save(r: Recipe, overwrite: bool):
            try:
                self.store.save_recipe(r, overwrite=overwrite)
            except RecipeExistsError as ex:
                ask_conflict(r, ex.existing_name)
                return
            except (OSError, ValueError) as ex:
                self._snack(f"Couldn't save: {ex}")
                return
            self.rc_name.value = r.name
            self._open_recipe_name = r.name
            # Remember every named ingredient so it autocompletes next time.
            try:
                self.library = self.store.add_many_to_library(r.ingredients)
            except (OSError, ValueError) as ex:
                self._snack(f"Recipe saved, but the ingredient library couldn't be updated: {ex}")
            self._refresh_saved_recipes()
            self.rc_saved.value = r.name
            self._show_store_warnings()
            self._snack(f"Saved '{r.name}'.")
            self.page.update()

        def ask_rename(r: Recipe):
            box = ft.TextField(label="New recipe name", value=self.store.suggest_unique_name(r.name), width=320)
            dlg = ft.AlertDialog(modal=True, title=ft.Text("Save under a new name"), content=box)

            def ok(_):
                new = " ".join((box.value or "").split())
                if not new:
                    box.error_text = "Type a name."
                    self.page.update()
                    return
                existing = self.store.find_recipe_name(new)
                if existing is not None:
                    box.error_text = f"'{existing}' is already saved — pick another name."
                    self.page.update()
                    return
                self._close(dlg)
                r.name = new
                do_save(r, overwrite=False)

            dlg.actions = [ft.TextButton("Cancel", on_click=lambda e: self._close(dlg)),
                           ft.ElevatedButton("Save", on_click=ok)]
            self._open(dlg)

        def ask_conflict(r: Recipe, existing: str):
            same = (self._open_recipe_name is not None and
                    normalize_recipe_name(existing) == normalize_recipe_name(self._open_recipe_name))
            if same:
                # Re-saving the recipe that is open: it's an update, not a clash
                # with some other recipe — but still never silent.
                title = f"Update '{existing}'?"
                body = (f"Save your changes over the saved copy of '{existing}', "
                        f"or keep that one and save this as a new recipe?")
                ok_label = f"Update '{existing}'"
            else:
                title = "That name is already saved"
                body = (f"You already have a different saved recipe called '{existing}'.\n"
                        f"Replace it with this one, or save this one under a new name?")
                ok_label = f"Replace '{existing}'"
            dlg = ft.AlertDialog(modal=True, title=ft.Text(title), content=ft.Text(body))

            def replace(_):
                self._close(dlg)
                do_save(r, overwrite=True)

            def rename(_):
                self._close(dlg)
                ask_rename(r)

            dlg.actions = [ft.TextButton("Cancel", on_click=lambda e: self._close(dlg)),
                           ft.OutlinedButton("Save as new name", on_click=rename),
                           ft.ElevatedButton(ok_label, on_click=replace)]
            self._open(dlg)

        def save(_=None):
            r, ok = collect_recipe()
            if not r.name:
                self.rc_name.error_text = "Give the recipe a name first."
                self.page.update()
                return
            self.rc_name.error_text = None
            if not ok:
                self._snack("Fix the boxes marked in red before saving.")
                self.page.update()
                return
            if not r.ingredients:
                self._snack("Add at least one ingredient before saving.")
                return
            do_save(r, overwrite=False)   # conflict -> dialog, never a silent overwrite

        def load(_=None):
            if not self.rc_saved.value:
                self._snack("Pick a recipe from the list first.")
                return
            r = self.store.load_recipe(self.rc_saved.value)
            if not r:
                self._show_store_warnings()
                self._snack(f"Couldn't open '{self.rc_saved.value}'.")
                self._refresh_saved_recipes()
                self.page.update()
                return
            self.rc_name.value = r.name
            self._open_recipe_name = r.name
            self.rc_name.error_text = None
            self.rc_yield.value = _fmt(r.yield_amount)
            self.rc_serving.value = _fmt(r.serving_size)
            self.rc_unit.value = r.unit
            self.rc_rows.controls.clear()
            for ing in r.ingredients:
                add_row(ing)
            if not r.ingredients:
                add_row()
            calc()

        def delete(_=None):
            name = self.rc_saved.value
            if not name:
                self._snack("Pick a recipe from the list first.")
                return

            def go():
                try:
                    self.store.delete_recipe(name)
                except OSError as ex:
                    self._snack(f"Couldn't delete '{name}': {ex}")
                    return
                if self._open_recipe_name and normalize_recipe_name(self._open_recipe_name) == \
                        normalize_recipe_name(name):
                    self._open_recipe_name = None
                self.rc_saved.value = None
                self._refresh_saved_recipes()
                self._snack(f"Deleted '{name}'.")
                self.page.update()

            self._confirm("Delete recipe?", f"Delete the saved recipe '{name}'? This can't be undone.",
                          "Delete", go)

        def new_recipe(_=None):
            self._open_recipe_name = None
            self.rc_name.value = ""
            self.rc_name.error_text = None
            self.rc_yield.value = "1"
            self.rc_serving.value = "1"
            self.rc_unit.value = "serving"
            for f in (self.rc_yield, self.rc_serving):
                f.error_text = None
            self.rc_rows.controls.clear()
            add_row()
            self.rc_result.value = ""
            self.rc_note.value = ""
            self.rc_shopping.controls = []
            self.page.update()

        for f in (self.rc_yield, self.rc_serving, self.rc_unit):
            f.on_change = calc
        self._recalcs.append(lambda: calc() if self.rc_result.value else None)
        add_row()

        return ft.Container(
            ft.Column(
                [
                    ft.Row([self.rc_name, self.rc_yield, self.rc_serving, self.rc_unit], wrap=True),
                    ft.Text(
                        "Ingredients — enter the macros for the TOTAL amount that goes in the pot "
                        "(not per serving). Total yield ÷ serving size = number of servings "
                        "(use the same unit for both, e.g. 64 oz and 8 oz). Ingredients you've saved "
                        "before pop up as you type the name.",
                        size=12, italic=True,
                    ),
                    self.rc_rows,
                    ft.Row(
                        [
                            ft.TextButton("+ Add ingredient", icon=ft.Icons.ADD, on_click=lambda e: add_row()),
                            ft.ElevatedButton("Calculate", icon=ft.Icons.CALCULATE, on_click=calc),
                            ft.OutlinedButton("Save recipe", icon=ft.Icons.SAVE, on_click=save),
                            ft.TextButton("New recipe", icon=ft.Icons.NOTE_ADD, on_click=new_recipe),
                        ],
                        wrap=True,
                    ),
                    ft.Row([self.rc_saved,
                            ft.TextButton("Open", icon=ft.Icons.FOLDER_OPEN, on_click=load),
                            ft.TextButton("Delete", icon=ft.Icons.DELETE, on_click=delete)], wrap=True),
                    ft.Divider(),
                    self.rc_result,
                    self.rc_note,
                    self.rc_shopping,
                ],
                spacing=10,
                scroll=ft.ScrollMode.AUTO,
            ),
            padding=20,
        )

    def _refresh_saved_recipes(self):
        names = self.store.list_recipes()
        self.rc_saved.options = [ft.dropdown.Option(n) for n in names]
        if self.rc_saved.value not in names:
            self.rc_saved.value = None

    # ------------------------------------------------------------------
    # Calibrate tab
    # ------------------------------------------------------------------
    def _calibrate_tab(self) -> ft.Container:
        self.cal_rows = ft.Column(spacing=6)
        self.cal_result = ft.Text("", size=13)
        self.cal_coeffs = ft.Text("", size=12, font_family="monospace", selectable=True)
        self.cal_apply = ft.OutlinedButton("Apply to profile", icon=ft.Icons.CHECK, disabled=True)

        def invalidate(_=None):
            """Any edit makes the last fit stale — it can no longer be applied."""
            if self._pending_result is not None:
                self._pending_result = None
                self.cal_apply.disabled = True
                self.cal_result.value = "Foods changed — press Fit coefficients again."
                self.cal_result.color = ft.Colors.BLUE_GREY_700
                self.cal_coeffs.value = ""
                self.page.update()

        def remove_cal_row(row):
            if row in self.cal_rows.controls:
                self.cal_rows.controls.remove(row)
            invalidate()
            self.page.update()

        def add_cal_row(sample: CalibrationSample | None = None):
            d = {"label": ft.TextField(label="Food", width=200, value=(sample.label if sample else ""))}
            for k in MACRO_KEYS:
                d[k] = ft.TextField(label=SHORT_LABELS[k], width=90, hint_text="0",
                                    value=(_fmt(getattr(sample.macros, k)) if sample else ""))
            d["obs"] = ft.TextField(label="WW Points", width=90,
                                    value=(_fmt(sample.observed_points) if sample else ""))
            for f in d.values():
                f.on_change = invalidate
            row = ft.Row([d["label"]] + [d[k] for k in MACRO_KEYS] + [d["obs"]], wrap=True)
            row.controls.append(ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip="Remove this food",
                                              on_click=lambda e, r=row: remove_cal_row(r)))
            row.data = d
            self.cal_rows.controls.append(row)
            invalidate()
            self.page.update()

        def collect_samples():
            out, ok = [], True
            for row in self.cal_rows.controls:
                d = row.data
                label = (d["label"].value or "").strip()
                vals = {}
                for k in MACRO_KEYS:
                    v, good = _read(d[k], units=MACRO_UNITS[k])
                    vals[k] = v
                    ok = ok and good
                obs, good_obs = _read(d["obs"], parse_number, empty_value=None, units=POINT_UNITS)
                if not label:
                    anything = any((d[k].value or "").strip() for k in MACRO_KEYS) or (d["obs"].value or "").strip()
                    if anything:
                        d["label"].error_text = "Name this food (or remove the row)."
                        ok = False
                    else:
                        d["label"].error_text = None
                        d["obs"].error_text = None
                    continue
                d["label"].error_text = None
                if good_obs and obs is not None and obs != int(obs):
                    d["obs"].error_text = "WW Points are whole numbers — type exactly what your WW app shows."
                    good_obs = False
                    obs = None
                if good_obs and obs and all(v == 0 for v in vals.values()):
                    d["calories"].error_text = "Type this food's label numbers (or remove the row)."
                    good_obs = False
                ok = ok and good_obs
                if ok and all(v is not None for v in vals.values()) and obs is not None:
                    out.append(CalibrationSample(label=label, macros=Macros(**vals), observed_points=obs))
            return out, ok

        def fit(_=None):
            samples, ok = collect_samples()
            self._pending_result = None
            self.cal_apply.disabled = True
            if not ok:
                self.cal_result.value = "Fix the box(es) marked in red, then Fit again."
                self.cal_result.color = ft.Colors.RED_700
                self.cal_coeffs.value = ""
                self.page.update()
                return
            if not samples:
                self.cal_result.value = ("Add your foods first — at least 5, each with the Points your WW app "
                                         "shows (or press Restore examples to see how it works).")
                self.cal_result.color = ft.Colors.RED_700
                self.cal_coeffs.value = ""
                self.page.update()
                return
            try:
                # Always regularize toward the FIXED reference (V4) — never the
                # current profile — so repeated Fit/Apply can't drift.
                result = calibrate(samples)
            except ImportError:
                self.cal_result.value = "Calibration needs numpy installed (pip install numpy)."
                self.cal_result.color = ft.Colors.RED_700
                self.cal_coeffs.value = ""
                self.page.update()
                return
            except (ValueError, ArithmeticError) as e:
                self.cal_result.value = str(e)
                self.cal_result.color = ft.Colors.RED_700
                self.cal_coeffs.value = ""
                self.page.update()
                return
            example = is_template_unchanged(samples)
            # The example readings ARE the reference: applying a fit of them
            # can only nudge the validated V4 numbers (it moves Papa John's
            # 90 -> 89), so Apply stays off until the user enters their own.
            self._pending_result = None if example else result
            self.cal_apply.disabled = example
            ref = Profile().coeffs
            lines = [f"  {'coefficient':14s} {'yours':>9s} {'reference':>10s}"]
            lines += [f"  {k:14s} {v:+9.5f} {ref[k]:+10.5f}" for k, v in result.coeffs.items()]
            self.cal_coeffs.value = "\n".join(lines)
            summary = (f"Matches {result.exact_matches} of {result.n_used} foods exactly "
                       f"(RMSE {result.rmse:.2f} pts).")
            if example:
                summary += ("  These are still the example readings (they match the reference "
                            "profile you already have), so Apply is off. Replace the WW Points "
                            "with the numbers YOUR WW app shows, then Fit again.")
            if result.stable:
                self.cal_result.value = f"Stable fit. {summary}" + ("" if example else
                                         " Review, then Apply to save to your profile.")
                if result.warning:
                    self.cal_result.value += "  " + result.warning
                self.cal_result.color = ft.Colors.GREEN_800
            else:
                self.cal_result.value = ("⚠ Not trustworthy yet: " + result.warning + "  " + summary +
                                         "  (A low RMSE on too-few or too-similar foods is an overfit, not a "
                                         "good fit.)")
                self.cal_result.color = ft.Colors.ORANGE_800
            self.page.update()

        def save_coeffs(result):
            self.profile.coeffs = dict(result.coeffs)
            self.profile.name = "Custom (calibrated)"
            try:
                self.store.save_profile(self.profile)
            except (OSError, ValueError) as ex:
                self._snack(f"Couldn't save the profile: {ex}")
                return
            self._pending_result = None
            self.cal_apply.disabled = True
            self._profile_changed()
            self._snack("Your calibrated coefficients are saved. All tabs now use them.")
            self.page.update()

        def apply(_=None):
            result = self._pending_result
            if result is None:
                self._snack("Press Fit coefficients first.")
                return
            if not result.stable:
                self._confirm("Apply an untrustworthy fit?",
                              "This fit was flagged as not trustworthy. Points on every tab will "
                              "change. You can undo with 'Reset to reference'. Apply anyway?",
                              "Apply anyway", lambda: save_coeffs(result))
            else:
                save_coeffs(result)

        def reset(_=None):
            def go():
                self.profile = self.store.reset_profile()
                self._profile_changed()
                self._snack("Back to the reference profile (V4). Your old profile was backed up.")
            self._confirm("Reset to reference?",
                          "Go back to the reference coefficients (V4) and default ZeroPoint list? "
                          "A backup of your current profile is kept in the data folder.",
                          "Reset", go)

        def restore_examples(_=None):
            self.cal_rows.controls.clear()
            for s in default_isolator_template():
                add_cal_row(s)
            invalidate()
            self.page.update()

        self.cal_apply.on_click = apply
        for s in default_isolator_template():
            add_cal_row(s)

        return ft.Container(
            ft.Column(
                [
                    ft.Text(
                        "Optional: calibrate to YOUR WW profile. For each food, type the label numbers and "
                        "the Points your WW app shows. Best foods: each high in ONE macro (cola, butter, "
                        "protein shake, bran cereal) plus a few everyday mixed foods. Don't use fiber "
                        "supplements or 0-Point foods.",
                        size=12, italic=True,
                    ),
                    ft.Text("Pre-filled with example readings; replace the WW Points with your app's numbers.",
                            size=11, color=ft.Colors.BLUE_GREY_600),
                    self.cal_rows,
                    ft.Row(
                        [
                            ft.TextButton("+ Add food", icon=ft.Icons.ADD, on_click=lambda e: add_cal_row()),
                            ft.ElevatedButton("Fit coefficients", icon=ft.Icons.FUNCTIONS, on_click=fit),
                            self.cal_apply,
                            ft.TextButton("Reset to reference", icon=ft.Icons.RESTORE, on_click=reset),
                            ft.TextButton("Restore examples", icon=ft.Icons.REFRESH, on_click=restore_examples),
                        ],
                        wrap=True,
                    ),
                    ft.Divider(),
                    self.cal_result,
                    self.cal_coeffs,
                ],
                spacing=10,
                scroll=ft.ScrollMode.AUTO,
            ),
            padding=20,
        )

    # ------------------------------------------------------------------
    # Help tab
    # ------------------------------------------------------------------
    def _help_tab(self) -> ft.Container:
        md = f"""
## How points are calculated

Each food's points come from a five-term linear model on its macros
(reference profile, V4):

```
Points = round( 0.03034·cal + 0.36869·sat_fat + 0.18540·added_sugar
                − 0.07317·protein − 0.11087·fiber )   (never below 0)
```

- Uses **added sugar** (not total) and **fiber** — this is the current
  **Points** program, not the older SmartPoints model.
- **ZeroPoint foods** log as **0** regardless of macros. A food whose name is
  on your ZeroPoint list is detected automatically (same name or a simple
  plural like "eggs", any case — on both tabs); a longer name such as
  "egg salad" only shows a hint. Use the toggle / checkbox for anything else.
- You can type numbers straight off the label: `12g`, `150 cal`, `1/2`,
  `1,5`. Recipe yield/serving accept a unit (`64 oz`, `8 oz`) — both must use
  the same unit; the app never converts units.
- Pure fiber supplements (e.g. Metamucil) can be off by 1 — a known limit.

## Recipe mode

Enter every ingredient's macros for the **amount used in the pot**, plus the
total yield and your serving size (same unit). Points/serving = the whole pot
scored, divided by servings, **rounded once at the end**. ZeroPoint
ingredients (ticked, or on your ZeroPoint list) contribute 0.

Saving asks before replacing a recipe with the same name. Every ingredient in
a saved recipe goes into your **ingredient library**: start typing its name
in any recipe row and click the suggestion to fill in the numbers.

## Calibrate to your own profile (optional)

Every WW member's profile can differ. On the **Calibrate** tab, log foods
with the Points your WW app shows. The fit is pulled gently toward the
reference so a few foods can't produce wild numbers, and it tells you when
your foods can't pin a coefficient down. **Apply** saves it; **Reset to
reference** undoes it.

## Your data

Stored only on this computer, in:
`{self.store.root}`
"""
        return ft.Container(
            ft.Column([ft.Markdown(md, selectable=True)], scroll=ft.ScrollMode.AUTO),
            padding=20,
        )


def main(page: ft.Page):
    WWApp(page)


if __name__ == "__main__":
    ft.app(target=main)
