"""WW Points Calculator — local JSON storage.

Everything persists to plain JSON files in a data folder (no server, fully
offline). Three stores:

    - profile.json    : the active coefficient Profile
    - library.json    : the reusable ingredient library (brand|name -> macros)
    - recipes/*.json  : one file per saved recipe

Safety rules:
  * Writes are atomic: write a temp file in the same folder, flush + fsync,
    then os.replace. A crash mid-save never corrupts an existing file.
    NaN/inf are refused at write time (allow_nan=False), so we never write
    JSON we can't read back.
  * A corrupt / wrong-shape file is QUARANTINED (copied to
    "<name>.corrupt-<timestamp>.json") before anything can overwrite it, and a
    plain-English warning is queued in Store.warnings for the UI to show.
    Nothing the user saved is ever silently thrown away.
  * Recipe filenames are collision-free: a short readable slug plus a hash of
    the normalized (trimmed, case-folded) name. Names that differ only by
    case/space/punctuation can no longer overwrite each other's file by
    accident, Windows reserved names (CON, NUL, COM1...) and very long names
    are safe, and path traversal is impossible. Recipes saved by v1 (old
    "My_Soup.json" filenames) still list, load and delete, and are migrated to
    the new filename the next time they are saved.
  * Recipe names are compared case-insensitively ("My Soup" == "my soup"),
    matching how Windows and macOS treat filenames.
  * save_recipe(overwrite=False) never replaces an existing recipe: it raises
    RecipeExistsError so the UI can ask Replace / Save as new name / Cancel.
    A damaged recipe file is quarantined, never silently overwritten.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import time
import unicodedata
from typing import Dict, List, Optional

from .engine import Profile, MACRO_KEYS
from .recipe import Recipe, Ingredient

APP_DIR_NAME = "WWPointsCalculator"



class RecipeExistsError(Exception):
    """save_recipe(overwrite=False) found a recipe with the same name."""

    def __init__(self, name: str, existing_name: str):
        super().__init__(f"A recipe named '{existing_name}' already exists.")
        self.name = name
        self.existing_name = existing_name


# ---------------------------------------------------------------------------
# low-level helpers
# ---------------------------------------------------------------------------

def _atomic_write(path: str, data) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    # Serialize FIRST so a bad value (NaN) raises before any file is touched.
    text = json.dumps(data, indent=2, allow_nan=False, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _dir_is_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path, suffix=".probe")
        os.close(fd)
        os.remove(tmp)
        return True
    except OSError:
        return False


def user_data_dir() -> str:
    """Per-user, always-writable data folder (fallback location)."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, APP_DIR_NAME)


def resolve_data_dir(preferred: str) -> str:
    """Use `preferred` (next to the app) if writable, else the per-user folder."""
    if _dir_is_writable(preferred):
        return preferred
    return user_data_dir()


def normalize_recipe_name(name: str) -> str:
    """Canonical form used for identity: NFC, trimmed, inner spaces collapsed, case-folded."""
    n = unicodedata.normalize("NFC", str(name or ""))
    return " ".join(n.split()).casefold()


def _recipe_filename(name: str) -> str:
    norm = normalize_recipe_name(name)
    slug = "".join(c for c in norm if c.isascii() and (c.isalnum() or c in " -_"))
    slug = "_".join(slug.split())[:40].strip("_-") or "recipe"
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}.json"


def _legacy_recipe_filename(name: str) -> str:
    """The v1 scheme (kept only to find/delete old files)."""
    safe = "".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).strip()
    safe = safe.replace(" ", "_") or "recipe"
    return f"{safe}.json"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    """Filesystem-backed store rooted at a data directory."""

    def __init__(self, root: str, *, fallback: bool = True):
        self.root = resolve_data_dir(root) if fallback else root
        self.recipes_dir = os.path.join(self.root, "recipes")
        os.makedirs(self.recipes_dir, exist_ok=True)
        self.warnings: List[str] = []
        if fallback and os.path.abspath(self.root) != os.path.abspath(root):
            self.warnings.append(
                f"The app folder isn't writable, so your data is saved in {self.root} instead."
            )

    def pop_warnings(self) -> List[str]:
        w, self.warnings = self.warnings, []
        return w

    def _quarantine(self, path: str, what: str, *, keep_original: bool = False) -> None:
        """Set a bad file aside (never delete it) and queue a warning.

        Default: MOVE it to the .corrupt- name, so the app starts clean and the
        same broken file isn't re-reported on every load. keep_original=True
        copies instead (caller rewrites a cleaned version itself).
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base, ext = os.path.splitext(path)
        dest = f"{base}.corrupt-{stamp}{ext or '.json'}"
        i = 1
        while os.path.exists(dest):
            dest = f"{base}.corrupt-{stamp}-{i}{ext or '.json'}"
            i += 1
        try:
            if keep_original:
                shutil.copy2(path, dest)
            else:
                os.replace(path, dest)
            self.warnings.append(
                f"Your {what} file was damaged and couldn't be fully read. The original was "
                f"kept at {dest}."
            )
        except OSError:
            self.warnings.append(f"Your {what} file was damaged and couldn't be read or backed up.")

    # -- profile -----------------------------------------------------------

    @property
    def profile_path(self) -> str:
        return os.path.join(self.root, "profile.json")

    @staticmethod
    def _profile_repairs(raw) -> List[str]:
        """What Profile.from_dict would have to silently replace in `raw`."""
        if not isinstance(raw, dict):
            return []  # whole file unusable -> handled by the quarantine path
        bad = []
        c = raw.get("coeffs")
        if not isinstance(c, dict):
            bad.append("all coefficients")
        else:
            for k in MACRO_KEYS:
                v = c.get(k)
                if not (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)):
                    bad.append(k.replace("_", " "))
        zp = raw.get("zero_point_foods")
        if zp is not None and not isinstance(zp, (list, str)):
            bad.append("ZeroPoint list")
        elif isinstance(zp, list) and any(not isinstance(z, str) for z in zp):
            bad.append("ZeroPoint list entries")
        return bad

    def load_profile(self) -> Profile:
        if not os.path.exists(self.profile_path):
            return Profile()
        try:
            with open(self.profile_path, encoding="utf-8") as f:
                raw = json.load(f)
            prof = Profile.from_dict(raw)
        except (ValueError, TypeError, AttributeError, OSError, UnicodeDecodeError):
            # ValueError covers json.JSONDecodeError. Keep a copy BEFORE the
            # user can save over it.
            self._quarantine(self.profile_path, "profile")
            return Profile()
        repairs = self._profile_repairs(raw)
        if repairs:
            # Never silently score with a half-reference, half-custom profile.
            self._quarantine(self.profile_path, "profile", keep_original=True)
            self.warnings[-1] += (f" Invalid value(s) for {', '.join(repairs)} were replaced with the "
                                  f"reference (V4) values — re-check your profile or use 'Reset to reference'.")
            try:
                self.save_profile(prof)
            except (OSError, ValueError):
                pass
        return prof

    def save_profile(self, profile: Profile) -> None:
        _atomic_write(self.profile_path, profile.to_dict())

    def reset_profile(self) -> Profile:
        """Back to the reference V4 profile (keeps a backup of the old file)."""
        if os.path.exists(self.profile_path):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            try:
                shutil.copy2(self.profile_path, os.path.join(self.root, f"profile.backup-{stamp}.json"))
            except OSError:
                pass
        p = Profile()
        self.save_profile(p)
        return p

    # -- ingredient library -----------------------------------------------

    @property
    def library_path(self) -> str:
        return os.path.join(self.root, "library.json")

    def load_library(self) -> Dict[str, Ingredient]:
        """Return {key -> Ingredient}, key = lowercased 'brand|name'.

        A damaged file is quarantined; any individually valid entries are
        still returned so the user keeps what can be read.
        """
        if not os.path.exists(self.library_path):
            return {}
        try:
            with open(self.library_path, encoding="utf-8") as f:
                raw = json.load(f)
        except (ValueError, OSError, UnicodeDecodeError):
            self._quarantine(self.library_path, "ingredient library")
            return {}
        if not isinstance(raw, dict):
            self._quarantine(self.library_path, "ingredient library")
            return {}
        out: Dict[str, Ingredient] = {}
        bad = 0
        for k, v in raw.items():
            try:
                ing = Ingredient.from_dict(v)
            except (ValueError, TypeError):
                bad += 1
                continue
            if not ing.name.strip():
                bad += 1
                continue
            out[self.library_key(ing.brand, ing.name)] = ing
        if bad:
            self._quarantine(self.library_path, "ingredient library", keep_original=True)
            self.warnings[-1] += (f" {bad} damaged entr{'y was' if bad == 1 else 'ies were'} skipped; "
                                  f"the rest of your library is fine.")
            try:
                self.save_library(out)   # cleaned copy; full original kept aside
            except (OSError, ValueError):
                pass
        return out

    def save_library(self, library: Dict[str, Ingredient]) -> None:
        data = {k: v.to_dict() for k, v in library.items()}
        _atomic_write(self.library_path, data)

    @staticmethod
    def library_key(brand: str, name: str) -> str:
        return f"{' '.join((brand or '').split()).casefold()}|{' '.join((name or '').split()).casefold()}"

    def add_to_library(self, ing: Ingredient) -> Dict[str, Ingredient]:
        return self.add_many_to_library([ing])

    def add_many_to_library(self, ings: List[Ingredient]) -> Dict[str, Ingredient]:
        """Upsert ingredients (by brand+name) in one atomic write. Blank names are skipped."""
        lib = self.load_library()
        changed = False
        for ing in ings:
            if not ing.name.strip():
                continue
            lib[self.library_key(ing.brand, ing.name)] = Ingredient(
                name=ing.name.strip(), brand=ing.brand.strip(), macros=ing.macros, is_zero=ing.is_zero)
            changed = True
        if changed:
            self.save_library(lib)
        return lib

    def search_library(self, query: str, limit: int = 8,
                       library: Optional[Dict[str, Ingredient]] = None) -> List[Ingredient]:
        """Library matches for an autocomplete box.

        Every word typed must appear in "brand name" (case-insensitive, any
        order). Entries whose name STARTS with the query rank first, then
        alphabetical. Empty query -> no suggestions.
        """
        words = (query or "").casefold().split()
        if not words:
            return []
        lib = self.load_library() if library is None else library
        hits = []
        for ing in lib.values():
            hay = f"{ing.brand} {ing.name}".casefold()
            if all(w in hay for w in words):
                starts = ing.name.casefold().startswith(" ".join(words))
                hits.append((0 if starts else 1, ing.name.casefold(), ing.brand.casefold(), ing))
        hits.sort(key=lambda t: t[:3])
        return [h[3] for h in hits[:max(0, int(limit))]]

    # -- recipes -----------------------------------------------------------

    def _recipe_path(self, name: str) -> str:
        return os.path.join(self.recipes_dir, _recipe_filename(name))

    def _iter_recipe_files(self):
        """Yield (path, stored_name) for every readable recipe file."""
        if not os.path.isdir(self.recipes_dir):
            return
        for fn in sorted(os.listdir(self.recipes_dir)):
            if not fn.endswith(".json") or ".corrupt-" in fn:
                continue
            path = os.path.join(self.recipes_dir, fn)
            d = self._read_recipe_json(path)
            if d is None:
                continue
            yield path, str(d.get("name") or fn[:-5])

    def _read_recipe_json(self, path: str):
        """Parsed recipe JSON (a dict), or None. An unreadable / non-object file
        is QUARANTINED (moved aside + warning) instead of being skipped
        silently — otherwise it would vanish from the list and the next save
        under that name would replace it without any conflict prompt."""
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return None
        except (ValueError, OSError, UnicodeDecodeError):
            d = None
        if isinstance(d, dict):
            return d
        self._quarantine(path, f"saved recipe ({os.path.basename(path)})")
        return None

    def find_recipe_name(self, name: str) -> Optional[str]:
        """The stored name of an existing recipe matching `name` (case-insensitive), or None."""
        target = normalize_recipe_name(name)
        if not target:
            return None
        for _path, stored in self._iter_recipe_files():
            if normalize_recipe_name(stored) == target:
                return stored
        return None

    def recipe_exists(self, name: str) -> bool:
        return self.find_recipe_name(name) is not None

    def suggest_unique_name(self, name: str) -> str:
        """'My Soup' -> 'My Soup (2)', '(3)', ... — first name not already saved.

        A trailing ' (N)' on the input is stripped first, so renaming
        'My Soup (2)' suggests 'My Soup (3)', not 'My Soup (2) (2)'.
        """
        import re
        base = " ".join(str(name or "").split()) or "Recipe"
        base = re.sub(r"\s\(\d+\)$", "", base) or base
        taken = {normalize_recipe_name(stored) for _p, stored in self._iter_recipe_files()}
        if normalize_recipe_name(base) not in taken:
            return base
        n = 2
        while normalize_recipe_name(f"{base} ({n})") in taken:
            n += 1
        return f"{base} ({n})"

    def list_recipes(self) -> List[str]:
        """Distinct recipe names (one entry per name, case-insensitive), sorted."""
        seen: Dict[str, str] = {}
        for _path, stored in self._iter_recipe_files():
            key = normalize_recipe_name(stored)
            if key and key not in seen:
                seen[key] = stored
        return sorted(seen.values(), key=lambda s: s.casefold())

    def _matching_files(self, name: str) -> List[str]:
        target = normalize_recipe_name(name)
        return [p for p, stored in self._iter_recipe_files() if normalize_recipe_name(stored) == target]

    def save_recipe(self, recipe: Recipe, *, overwrite: bool = True) -> str:
        """Save a recipe; returns the path written.

        overwrite=False: raise RecipeExistsError if a recipe with the same
        (case-insensitive) name already exists, so the UI can ask
        "Replace or rename?". overwrite=True replaces it.
        Old-scheme (v1) files for the same name are removed AFTER the new
        file is safely written (migration, no duplicate entries).
        """
        name = " ".join(str(recipe.name or "").split())
        if not name:
            raise ValueError("Recipe needs a name.")
        recipe.name = name
        if not overwrite:
            existing = self.find_recipe_name(name)
            if existing is not None:
                raise RecipeExistsError(name, existing)
        path = self._recipe_path(name)
        if os.path.exists(path):
            # Never write over a damaged file at the target path: set it aside first.
            self._read_recipe_json(path)
        old_files = [p for p in self._matching_files(name) if os.path.abspath(p) != os.path.abspath(path)]
        for p in [path] + old_files:
            # Readable JSON that isn't a loadable recipe (hand-edited, bad
            # values): the user may pick "Replace" without knowing it was
            # damaged — keep a copy of what we're about to replace.
            d = self._read_recipe_json(p) if os.path.exists(p) else None
            if d is not None:
                try:
                    Recipe.from_dict(d)
                except (ValueError, TypeError):
                    self._quarantine(p, f"saved recipe '{d.get('name') or os.path.basename(p)}'",
                                     keep_original=True)
        _atomic_write(path, recipe.to_dict())
        for p in old_files:
            try:
                os.remove(p)
            except OSError:
                pass
        return path

    def load_recipe(self, name: str) -> Recipe | None:
        """Load by name (case-insensitive). Returns None if missing or unreadable."""
        candidates, seen = [], set()
        for p in [self._recipe_path(name)] + self._matching_files(name):
            if os.path.abspath(p) not in seen:
                seen.add(os.path.abspath(p))
                candidates.append(p)
        for path in candidates:
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                r = Recipe.from_dict(d)
            except (ValueError, TypeError, OSError, UnicodeDecodeError):
                self.warnings.append(f"The saved recipe '{name}' is damaged and couldn't be opened.")
                continue
            if normalize_recipe_name(r.name) == normalize_recipe_name(name):
                return r
        return None

    def delete_recipe(self, name: str) -> bool:
        removed = False
        for p in set([self._recipe_path(name)] + self._matching_files(name)):
            if os.path.exists(p):
                os.remove(p)
                removed = True
        return removed
