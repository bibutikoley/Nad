#!/usr/bin/env python3
"""Build data/drugs.txt from the FDA's Drugs@FDA data files.

Input is the zip the FDA publishes at https://www.fda.gov/media/89850/download (weekly), or
the Products.txt inside it. Every approved product's brand name and active ingredient(s)
become lexicon entries, spelled the way a transcript should show them: brands in Title
Case (Lipitor), ingredients in lowercase (atorvastatin), salts stripped ("amitriptyline
hydrochloride" -> "amitriptyline") because that is the word people say. Combination
ingredients ("acetaminophen; codeine phosphate") are split. Names with digits, symbols or
more than four words (strengths, "IN PLASTIC CONTAINER", radiopharmaceuticals) are dropped.

Usage:  uv run --no-project scripts/build_drug_lexicon.py path/to/drugsatfda.zip
        uv run --no-project scripts/build_drug_lexicon.py Products.txt -o data/drugs.txt

Stdlib only. Nothing here is downloaded: fetch the zip yourself, this only reads it.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import re
import sys
import zipfile
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "drugs.txt"

# Trailing words that name the salt/ester/hydrate form rather than the drug. Stripped
# repeatedly ("bupropion hydrochloride monohydrate" -> "bupropion"). The stripped form is
# added *alongside* the full one, so "lithium carbonate" still matches when spoken whole.
SALTS = {
    "acetate", "anhydrous", "benzoate", "besylate", "bitartrate", "bromide", "calcium",
    "carbonate", "chloride", "citrate", "cypionate", "decanoate", "dihydrate", "dipropionate",
    "disodium", "enanthate", "estolate", "ethanolate", "fumarate", "furoate", "gluconate",
    "hemihydrate", "hyclate", "hydrate", "hydrobromide", "hydrochloride", "hydroxide",
    "lactate", "magnesium", "malate", "maleate", "mesylate", "monohydrate", "nitrate",
    "oxalate", "oxide", "palmitate", "pamoate", "phosphate", "potassium", "propionate",
    "salicylate", "sesquihydrate", "sodium", "stearate", "succinate", "sulfate", "tartrate",
    "tosylate", "trihydrate", "valerate",
}
# Two-word salt tails ("sodium phosphate", "hydrochloride monohydrate") fall out of the
# repeated single-word stripping above; nothing special needed.

MAX_WORDS = 4
_CLEAN = re.compile(r"^[A-Za-z][A-Za-z '\-]*$")


def strip_salt(name: str) -> str:
    words = name.split()
    while len(words) > 1 and words[-1] in SALTS:
        words.pop()
    return " ".join(words)


def clean(raw: str) -> str | None:
    """Normalise one FDA field value to a lexicon name, or None if it is not a name."""
    s = " ".join(raw.replace(",", " ").split()).strip(" -")
    if not s or not _CLEAN.match(s) or len(s.split()) > MAX_WORDS:
        return None
    return s.lower()


def read_products(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            member = next(n for n in z.namelist() if n.lower().endswith("products.txt"))
            text = z.read(member).decode("cp1252", errors="replace")
    else:
        text = path.read_text(encoding="cp1252", errors="replace")
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def build(rows: list[dict[str, str]]) -> tuple[list[str], list[str]]:
    """(brands, ingredients): deduplicated, sorted, display-cased."""
    ingredients: set[str] = set()
    brands: set[str] = set()
    for row in rows:
        for part in (row.get("ActiveIngredient") or "").split(";"):
            if (ing := clean(part)) is not None:
                ingredients.add(ing)
                ingredients.add(strip_salt(ing))
        if (name := clean(row.get("DrugName") or "")) is not None:
            brands.add(name)
    # A "brand" that is just the ingredient(s) -- generic products list the generic, or
    # "x and y" for a combination, as DrugName -- is not a brand.
    def is_generic(name: str) -> bool:
        return all(strip_salt(p.strip()) in ingredients for p in name.split(" and "))

    return (
        sorted(title(b) for b in brands if not is_generic(b)),
        sorted(ingredients),
    )


# Release-form suffixes that are initialisms, not words.
_UPPER = {"xl", "xr", "sr", "er", "cr", "cd", "la", "odt", "hfa", "hct", "ds", "cq", "pm", "d"}


def title(name: str) -> str:
    return " ".join(w.upper() if w in _UPPER else w.capitalize() for w in name.split())


def render(brands: list[str], ingredients: list[str], *, source: str) -> str:
    today = dt.datetime.now(tz=dt.UTC).date().isoformat()
    head = [
        f"# Built by scripts/build_drug_lexicon.py on {today} from {source}.",
        "# One name per line; `#` comments and blank lines are ignored. Regenerate rather",
        "# than hand-edit -- or add a second file and add it to MEDICAL_LEXICON.",
        "",
        f"# --- active ingredients ({len(ingredients)}) ---",
        *ingredients,
    ]
    if not brands:
        return "\n".join(head + [""])
    return "\n".join(head + ["", f"# --- brand names ({len(brands)}) ---", *brands, ""])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", type=Path, help="Drugs@FDA zip, or its Products.txt")
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    # Brands are excluded from the shipped lexicon on purpose, and this is how it is
    # regenerated. Measured on 101 ordinary sentences: the 3.5k ingredients corrupt none of
    # them, the 5.2k brands corrupt 6 ("for the" -> Forteo, "the oven" -> Theovent, "violin"
    # -> V-cillin). Brand names are short and English-shaped in a way generic names are not,
    # so no threshold separates them; the fix is not to load them wholesale. The few dozen
    # patients actually say are hand-kept in data/drugs.txt.
    ap.add_argument(
        "--no-brands",
        action="store_true",
        help="emit active ingredients only (what data/ingredients.txt ships as)",
    )
    args = ap.parse_args(argv)

    rows = read_products(args.source)
    brands, ingredients = build(rows)
    if args.no_brands:
        brands = []
    args.out.write_text(render(brands, ingredients, source=args.source.name))
    print(
        f"{args.out}: {len(ingredients)} ingredients, {len(brands)} brands "
        f"from {len(rows)} products"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
