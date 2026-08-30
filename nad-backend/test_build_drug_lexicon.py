"""Tests for the Drugs@FDA -> data/drugs.txt build, on a hand-made Products.txt."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "build_drug_lexicon", Path(__file__).with_name("scripts") / "build_drug_lexicon.py"
)
bdl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bdl)

_ROWS = [
    "ApplNo\tProductNo\tForm\tStrength\tReferenceDrug\tDrugName\tActiveIngredient\tReferenceStandard",
    "020702\t001\tTABLET;ORAL\t10MG\t1\tLIPITOR\tATORVASTATIN CALCIUM\t1",
    "076477\t001\tTABLET;ORAL\t10MG\t0\tATORVASTATIN CALCIUM\tATORVASTATIN CALCIUM\t0",
    "088016\t001\tTABLET;ORAL\t500MG\t0\tMETFORMIN HYDROCHLORIDE\tMETFORMIN HYDROCHLORIDE\t0",
    "040099\t001\tTABLET;ORAL\t300MG;30MG\t0\tACETAMINOPHEN AND CODEINE PHOSPHATE\tACETAMINOPHEN; CODEINE PHOSPHATE\t0",
    "018936\t001\tINJECTABLE;INJECTION\t5%\t0\tDEXTROSE 5% IN PLASTIC CONTAINER\tDEXTROSE\t0",
    "018658\t001\tINJECTABLE;INJECTION\t0.9%\t0\tSODIUM CHLORIDE\tSODIUM CHLORIDE\t0",
    "021871\t001\tTABLET;ORAL\t150MG\t1\tWELLBUTRIN XL\tBUPROPION HYDROCHLORIDE\t1",
    "022345\t001\tTABLET;ORAL\t2.5MG\t1\tELIQUIS\tAPIXABAN\t1",
]
PRODUCTS = "\r\n".join(_ROWS)


@pytest.fixture
def products_txt(tmp_path: Path) -> Path:
    p = tmp_path / "Products.txt"
    p.write_bytes(PRODUCTS.encode("cp1252"))
    return p


def test_reads_the_zip_or_the_txt(products_txt: Path, tmp_path: Path) -> None:
    z = tmp_path / "drugsatfda.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Products.txt", products_txt.read_bytes())
        zf.writestr("Applications.txt", "ignored")
    assert bdl.read_products(z) == bdl.read_products(products_txt)


def test_ingredients_are_lowercase_split_and_salt_stripped(products_txt: Path) -> None:
    _, ingredients = bdl.build(bdl.read_products(products_txt))
    assert "atorvastatin" in ingredients and "atorvastatin calcium" in ingredients
    assert "metformin" in ingredients
    assert "acetaminophen" in ingredients and "codeine" in ingredients
    assert "bupropion" in ingredients
    assert "sodium chloride" in ingredients  # nothing left after stripping -> kept whole


def test_brands_are_title_case_and_exclude_generics(products_txt: Path) -> None:
    brands, _ = bdl.build(bdl.read_products(products_txt))
    assert brands == ["Eliquis", "Lipitor", "Wellbutrin XL"]
    # "ATORVASTATIN CALCIUM" and "ACETAMINOPHEN AND CODEINE PHOSPHATE" as DrugNames are
    # generic products, not brands; their ingredients are already in the other list.
    assert "Atorvastatin Calcium" not in brands


def test_junk_names_are_dropped() -> None:
    assert bdl.clean("DEXTROSE 5% IN PLASTIC CONTAINER") is None
    assert bdl.clean("TECHNETIUM TC-99M SESTAMIBI KIT") is None
    assert bdl.clean("ONE TWO THREE FOUR FIVE") is None
    assert bdl.clean("  ") is None
    assert bdl.clean("HYDROCODONE BITARTRATE AND CAFFEINE") == "hydrocodone bitartrate and caffeine"


def test_render_is_a_readable_lexicon(products_txt: Path, tmp_path: Path) -> None:
    from drug_lexicon import DrugLexicon

    out = tmp_path / "drugs.txt"
    assert bdl.main([str(products_txt), "-o", str(out)]) == 0
    lx = DrugLexicon.from_file(out)
    assert len(lx) > 8
    assert lx.correct("I take met formin and lip a tore") == "I take metformin and Lipitor"
