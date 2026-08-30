#!/usr/bin/env python3
"""Score how much medical vocabulary survives the pipeline, on real audio.

Everything in test_drug_lexicon.py is a *hand-written* mangling: someone guessed how the
model would mishear "atorvastatin". That is enough to tune a threshold and not enough to
claim an accuracy number. This measures the real thing.

    # 1. Read data/calibration.txt aloud, one line per turn, with the dump on:
    NAD_AUDIO_DUMP=/tmp/nad-medical scripts/dev.sh
    # 2. Score what the STT actually produced:
    uv run --group dev scripts/medical_eval.py /tmp/nad-medical

The dumped `segNNNN.txt` is the transcript *before* the lexicon runs (noise_gate.py writes
it, DrugCorrectedSTT corrects afterwards), so one dump measures both stages: what the model
heard, and what the lexicon then made of it. That is the whole point -- it separates "the
STT got it wrong" from "the lexicon failed to fix it" from "the lexicon broke it".

Reported per term, since a whole-utterance WER hides exactly the failure that matters here:

    raw        the term appeared, spelled correctly, straight from the STT
    recovered  the STT missed it and the lexicon put it back    <- what the lexicon is for
    lost       neither; the term did not survive                <- real defects
    corrupted  a control line with no medical content got a correction  <- worse than lost

With --stt the wavs are re-transcribed against a running server instead of using the dumped
text, which is how to compare two STT models on one recording without speaking twice.

Exit status is 1 if anything was corrupted or lost, so this can gate a change.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drug_lexicon import DEFAULT_LEXICONS, DrugLexicon  # noqa: E402

DEFAULT_CALIBRATION = Path(__file__).resolve().parent.parent / "data" / "calibration.txt"
_TERM = re.compile(r"\[\[(.+?)\]\]")
# Alignment floor. Two readings of the same sentence rarely fall below this even when the
# medical term inside is mangled beyond recognition, because the carrier words match.
MATCH_FLOOR = 0.45


@dataclass
class Prompt:
    """One calibration line: what to say, and what must survive saying it."""

    spoken: str
    terms: list[str]
    heard: str | None = None
    corrected: str | None = None

    @property
    def is_control(self) -> bool:
        return not self.terms


@dataclass
class Result:
    raw: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    lost: list[tuple[str, str]] = field(default_factory=list)
    corrupted: list[tuple[str, str, str]] = field(default_factory=list)
    unmatched: list[Prompt] = field(default_factory=list)


def parse_calibration(path: Path) -> list[Prompt]:
    prompts = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        terms = _TERM.findall(line)
        prompts.append(Prompt(spoken=_TERM.sub(r"\1", line), terms=terms))
    return prompts


def read_dump(directory: Path) -> list[tuple[Path, str]]:
    """(wav, transcript) for each dumped segment, in the order they were spoken."""
    out = []
    for wav in sorted(directory.glob("seg*.wav")):
        txt = wav.with_suffix(".txt")
        out.append((wav, txt.read_text().strip() if txt.exists() else ""))
    return out


def transcribe(wav: Path, base_url: str, model: str) -> str:
    """POST one wav to an OpenAI-compatible /v1/audio/transcriptions."""
    boundary = uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n".encode(),
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f'filename="{wav.name}"\r\nContent-Type: audio/wav\r\n\r\n'
        ).encode(),
        wav.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(
        base_url.rstrip("/") + "/audio/transcriptions",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp).get("text", "").strip()


def align(prompts: list[Prompt], heard: list[str]) -> None:
    """Attach each transcript to the calibration line it is most likely a reading of.

    Not zip(): the noise gate drops segments, a cough splits one line into two, and the
    reader loses their place. Greedy best-similarity assignment instead, each transcript
    used once, anything below MATCH_FLOOR left unassigned and reported rather than scored.
    """
    pairs = sorted(
        (
            (SequenceMatcher(None, p.spoken.lower(), h.lower()).ratio(), i, j)
            for i, p in enumerate(prompts)
            for j, h in enumerate(heard)
        ),
        reverse=True,
    )
    used_p: set[int] = set()
    used_h: set[int] = set()
    for score, i, j in pairs:
        if score < MATCH_FLOOR or i in used_p or j in used_h:
            continue
        used_p.add(i)
        used_h.add(j)
        prompts[i].heard = heard[j]


def score(prompts: list[Prompt], lexicon: DrugLexicon) -> Result:
    res = Result()
    for p in prompts:
        if p.heard is None:
            res.unmatched.append(p)
            continue
        p.corrected = lexicon.correct(p.heard)
        if p.is_control:
            if p.corrected != p.heard:
                res.corrupted.append((p.spoken, p.heard, p.corrected))
            continue
        for term in p.terms:
            in_raw = term.lower() in p.heard.lower()
            in_corrected = term.lower() in p.corrected.lower()
            if in_raw:
                res.raw.append(term)
            elif in_corrected:
                res.recovered.append(term)
            else:
                res.lost.append((term, p.heard))
    return res


def report(res: Result, total_terms: int) -> None:
    captured = len(res.raw) + len(res.recovered)
    scored = captured + len(res.lost)
    print("=" * 72)
    print("MEDICAL TERM CAPTURE")
    print("=" * 72)
    if scored:
        pct = 100 * captured / scored
        print(f"  captured   {captured:3d}/{scored}  ({pct:.1f}%)")
        print(f"    raw from STT   {len(res.raw):3d}")
        print(f"    recovered by lexicon {len(res.recovered):3d}")
        print(f"  lost       {len(res.lost):3d}")
    else:
        print("  nothing scored -- no calibration line matched a transcript")
    print(f"  corrupted  {len(res.corrupted):3d}  (control lines altered)")

    if res.recovered:
        print("\n--- recovered by the lexicon (it earned its keep here) ---")
        for t in res.recovered:
            print(f"  {t}")
    if res.lost:
        print("\n--- LOST: spoken but never captured ---")
        for term, heard in res.lost:
            print(f"  {term}\n      heard: {heard!r}")
    if res.corrupted:
        print("\n--- CORRUPTED: non-medical speech was 'corrected' ---")
        for spoken, heard, corrected in res.corrupted:
            print(f"  said : {spoken}")
            print(f"  heard: {heard}")
            print(f"  ->   : {corrected}")
    if res.unmatched:
        print(f"\n--- {len(res.unmatched)} calibration lines had no matching audio ---")
        for p in res.unmatched[:10]:
            print(f"  {p.spoken}")
        print("  (skipped a line, or the gate rejected it -- not scored either way)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dump", type=Path, help="NAD_AUDIO_DUMP directory")
    ap.add_argument("-c", "--calibration", type=Path, default=DEFAULT_CALIBRATION)
    ap.add_argument(
        "--stt",
        metavar="BASE_URL",
        help="re-transcribe the wavs here (e.g. http://localhost:8001/v1) "
        "instead of scoring the dumped text",
    )
    ap.add_argument("--stt-model", default="omi", help="model field for --stt")
    ap.add_argument(
        "--lexicon",
        type=Path,
        nargs="*",
        default=list(DEFAULT_LEXICONS),
        help="lexicon files (default: the three agent.py loads)",
    )
    args = ap.parse_args(argv)

    if not args.dump.is_dir():
        print(f"no such dump directory: {args.dump}", file=sys.stderr)
        return 2
    prompts = parse_calibration(args.calibration)
    segments = read_dump(args.dump)
    if not segments:
        print(f"no seg*.wav in {args.dump}", file=sys.stderr)
        return 2

    if args.stt:
        heard = []
        for wav, _ in segments:
            try:
                heard.append(transcribe(wav, args.stt, args.stt_model))
            except (urllib.error.URLError, TimeoutError) as exc:
                print(f"{wav.name}: {exc}", file=sys.stderr)
                heard.append("")
    else:
        heard = [t for _, t in segments]

    lexicon = DrugLexicon.from_files(*args.lexicon)
    print(f"lexicon: {len(lexicon)} names from {', '.join(p.name for p in args.lexicon)}")
    print(f"prompts: {len(prompts)} calibration lines, {len(segments)} recorded segments\n")

    align(prompts, heard)
    res = score(prompts, lexicon)
    report(res, sum(len(p.terms) for p in prompts))
    return 1 if (res.lost or res.corrupted) else 0


if __name__ == "__main__":
    raise SystemExit(main())
