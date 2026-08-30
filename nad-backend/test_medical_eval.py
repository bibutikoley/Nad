"""Tests for the real-audio scorer.

The scorer is the thing that will eventually say whether medical capture is good enough,
so it has to be trustworthy itself: a scorer that quietly counts a lost term as captured
is worse than no scorer. These fix its arithmetic and, mostly, its alignment -- the part
with real room to be wrong, since it guesses which calibration line each recording is.

Run:  uv run --group dev pytest test_medical_eval.py
"""

from __future__ import annotations

import importlib.util
import sys
import wave
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "medical_eval", Path(__file__).with_name("scripts") / "medical_eval.py"
)
assert _spec and _spec.loader
medical_eval = importlib.util.module_from_spec(_spec)
# Registered before exec: @dataclass resolves annotations through sys.modules, and the
# module is not importable by name from here (it lives in scripts/, not on the path).
sys.modules["medical_eval"] = medical_eval
_spec.loader.exec_module(medical_eval)

from drug_lexicon import DEFAULT_LEXICONS, DrugLexicon, fold  # noqa: E402


@pytest.fixture(scope="module")
def lexicon() -> DrugLexicon:
    return DrugLexicon.from_files(*DEFAULT_LEXICONS)


def write_dump(directory: Path, transcripts: list[str]) -> Path:
    for i, text in enumerate(transcripts):
        wav = directory / f"seg{i:04d}.wav"
        with wave.open(str(wav), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(b"\x00\x00" * 100)
        wav.with_suffix(".txt").write_text(text)
    return directory


def test_parse_calibration_splits_prompt_from_terms(tmp_path: Path) -> None:
    f = tmp_path / "cal.txt"
    f.write_text("# comment\n\nI take [[metformin]] daily.\nJust an ordinary line.\n")
    prompts = medical_eval.parse_calibration(f)
    assert [p.spoken for p in prompts] == ["I take metformin daily.", "Just an ordinary line."]
    assert prompts[0].terms == ["metformin"]
    # No brackets means a control line: it is scored for corruption, not for capture.
    assert prompts[1].terms == []
    assert prompts[1].is_control


def test_multiple_terms_on_one_line(tmp_path: Path) -> None:
    f = tmp_path / "cal.txt"
    f.write_text("She takes [[losartan]], not [[valsartan]].\n")
    (p,) = medical_eval.parse_calibration(f)
    assert p.terms == ["losartan", "valsartan"]
    assert p.spoken == "She takes losartan, not valsartan."


def test_scores_raw_recovered_and_lost(tmp_path: Path, lexicon: DrugLexicon) -> None:
    cal = tmp_path / "cal.txt"
    cal.write_text(
        "I take [[metformin]] twice a day with food.\n"
        "He has been on [[lisinopril]] for about three years.\n"
        "She is on [[dupilumab]] for her skin.\n"
    )
    prompts = medical_eval.parse_calibration(cal)
    medical_eval.align(
        prompts,
        [
            "I take met formin twice a day with food",  # lexicon recovers it
            "He has been on lisinopril for about three years",  # STT got it
            "She is on do pillow map for her skin",  # too far gone for the lexicon
        ],
    )
    res = medical_eval.score(prompts, lexicon)
    assert res.raw == ["lisinopril"]
    assert res.recovered == ["metformin"]
    assert [t for t, _ in res.lost] == ["dupilumab"]
    assert res.corrupted == []


def test_control_line_corruption_is_reported(tmp_path: Path) -> None:
    # A lexicon that will definitely fire on a control line, to prove the check works
    # rather than passing because the real lexicon happens to be clean.
    cal = tmp_path / "cal.txt"
    cal.write_text("The tour guide explained the local customs.\n")
    prompts = medical_eval.parse_calibration(cal)
    medical_eval.align(prompts, ["The tour guide explained the local customs"])
    res = medical_eval.score(prompts, DrugLexicon(["Theo-dur"]))
    assert len(res.corrupted) == 1
    assert "Theo-dur" in res.corrupted[0][2]


def test_alignment_survives_a_dropped_and_reordered_segment(tmp_path: Path) -> None:
    # The gate drops segments and readers lose their place, so alignment must not be zip().
    cal = tmp_path / "cal.txt"
    cal.write_text(
        "I take [[metformin]] twice a day with food.\n"
        "He has been on [[lisinopril]] for about three years.\n"
        "The scan confirmed a [[pulmonary embolism]].\n"
    )
    prompts = medical_eval.parse_calibration(cal)
    # Line 2 never made it to disk, and the other two arrived out of order.
    medical_eval.align(
        prompts,
        ["The scan confirmed a pulmonary embolism", "I take metformin twice a day with food"],
    )
    assert prompts[0].heard == "I take metformin twice a day with food"
    assert prompts[1].heard is None
    assert prompts[2].heard == "The scan confirmed a pulmonary embolism"


def test_unrelated_audio_is_left_unscored(tmp_path: Path, lexicon: DrugLexicon) -> None:
    # Below MATCH_FLOOR nothing is assigned: an unmatched line is reported, never counted
    # as a loss, because we cannot tell whether it was even spoken.
    cal = tmp_path / "cal.txt"
    cal.write_text("I take [[metformin]] twice a day with food.\n")
    prompts = medical_eval.parse_calibration(cal)
    medical_eval.align(prompts, ["completely unrelated chatter about the weather"])
    res = medical_eval.score(prompts, lexicon)
    assert res.unmatched and not res.lost


def test_main_exits_nonzero_when_a_term_is_lost(tmp_path: Path) -> None:
    cal = tmp_path / "cal.txt"
    cal.write_text("She is on [[dupilumab]] for her skin.\n")
    dump = write_dump(tmp_path, ["She is on do pillow map for her skin"])
    assert medical_eval.main([str(dump), "-c", str(cal)]) == 1


def test_main_exits_zero_on_a_clean_run(tmp_path: Path) -> None:
    cal = tmp_path / "cal.txt"
    cal.write_text("I take [[metformin]] twice a day with food.\n")
    dump = write_dump(tmp_path, ["I take met formin twice a day with food"])
    assert medical_eval.main([str(dump), "-c", str(cal)]) == 0


def test_shipped_calibration_is_wellformed() -> None:
    prompts = medical_eval.parse_calibration(medical_eval.DEFAULT_CALIBRATION)
    assert len(prompts) > 50
    controls = [p for p in prompts if p.is_control]
    assert len(controls) >= 5, "controls are what detect corruption; keep some"
    # Every bracketed term must be one the lexicon actually knows. Scoring a term the
    # pipeline was never equipped to capture would report a defect that is really a gap in
    # the calibration, and the lexicon can only ever recover what it has a name for.
    lexicon = DrugLexicon.from_files(*DEFAULT_LEXICONS)
    unknown = [t for p in prompts for t in p.terms if lexicon.best(fold(t)) is None]
    assert not unknown, f"calibration terms missing from the lexicon: {unknown}"
