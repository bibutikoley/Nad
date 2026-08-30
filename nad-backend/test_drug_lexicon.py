"""Tests for the drug-name corrector.

The "heard" strings are hand-written approximations of how a transducer splits a drug
name it does not know into English-looking pieces. Replace them with real misfires from
NAD_AUDIO_DUMP transcripts as they turn up; THRESHOLD and the folds were set on these.

Run:  uv run --group dev pytest test_drug_lexicon.py
"""

from __future__ import annotations

import pytest

from drug_lexicon import _ENGLISH, DEFAULT_LEXICONS, DrugLexicon, fold, skeleton


def drug_lexicon_english() -> frozenset[str]:
    return _ENGLISH


@pytest.fixture(scope="module")
def lexicon() -> DrugLexicon:
    return DrugLexicon.from_file()


def test_lexicon_loads_and_skips_comments(lexicon: DrugLexicon) -> None:
    assert len(lexicon) > 200
    assert lexicon.correct("metformin") == "metformin"


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("I take met formin twice a day", "I take metformin twice a day"),
        ("she is on a tour of statin", "she is on atorvastatin"),
        ("lice in a pril for blood pressure", "lisinopril for blood pressure"),
        ("my doctor gave me lip a tore", "my doctor gave me Lipitor"),
        ("insulin glar jean at night", "insulin glargine at night"),
        ("he takes am low dipping", "he takes amlodipine"),
        ("give him a moxy cillin", "give him amoxicillin"),
        ("prescribed hydro chloro thigh a zide", "prescribed hydrochlorothiazide"),
        ("the medication was as pirin", "the medication was aspirin"),
        ("oh zempic weekly", "Ozempic weekly"),
        ("cypro flocks a sin", "ciprofloxacin"),
        ("oh me pray zole", "omeprazole"),
        ("metformin and met formin", "metformin and metformin"),
    ],
)
def test_corrects_misheard_names(lexicon: DrugLexicon, heard: str, expected: str) -> None:
    assert lexicon.correct(heard) == expected


@pytest.mark.parametrize(
    "text",
    [
        "I'm on Metformin and lisinopril.",  # already right, including capitals
        "the patient is allergic to penicillin and sulfa",
        "I forgot to mention the levothyroxine",
        "we took a tour of the city",
        "the meeting is at four, please be on time",
        "I have a headache and a fever",
        "let's go over the plan for the quarter",
        "my sister in law is visiting",
        "take one Tylenol",
        "",
    ],
)
def test_leaves_ordinary_and_correct_text_alone(lexicon: DrugLexicon, text: str) -> None:
    assert lexicon.correct(text) == text


def test_never_swallows_the_neighbouring_word(lexicon: DrugLexicon) -> None:
    # "on Metformin" scores 0.82 against metformin -- above threshold -- but the window
    # is only an extension of the exact match inside it, so "on" must survive.
    assert lexicon.correct("I'm on Metformin daily") == "I'm on Metformin daily"


def test_short_english_phrase_near_a_drug_name(lexicon: DrugLexicon) -> None:
    # Was an xfail: "a new statin" scored 0.8+ against nystatin. SHORT_WINDOW_THRESHOLD
    # fixed it as a side effect of holding short windows to a higher bar.
    assert lexicon.correct("I need a new statin") == "I need a new statin"


def test_find_reports_spans(lexicon: DrugLexicon) -> None:
    (c,) = lexicon.find("I take met formin twice a day")
    assert (c.heard, c.drug) == ("met formin", "metformin")
    assert "I take met formin twice a day"[c.start : c.end] == "met formin"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("lice", "lise"),
        ("cillin", "silin"),
        ("Lipitor", "lipitor"),
        ("phenol", "fenol"),
        ("Xanax", "ksanaks"),
        ("aspirin", "aspirin"),
    ],
)
def test_fold(a: str, b: str) -> None:
    assert fold(a) == fold(b)


# --- conditions -------------------------------------------------------------------
#
# data/conditions.txt is loaded alongside data/drugs.txt in production (agent.py's
# MEDICAL_LEXICON), so the tests that matter are run against the combination: adding
# ~370 names to the fuzzy blocks is exactly what could start capturing plain English.


@pytest.fixture(scope="module")
def combined() -> DrugLexicon:
    return DrugLexicon.from_files(*DEFAULT_LEXICONS)


def test_combined_lexicon_loads_every_default_file(combined: DrugLexicon) -> None:
    assert len(combined) > 4000
    # One name from each of the three files, spelled canonically, round-trips untouched.
    assert combined.correct("metformin") == "metformin"  # drugs.txt
    assert combined.correct("dupilumab") == "dupilumab"  # ingredients.txt
    assert combined.correct("atrial fibrillation") == "atrial fibrillation"  # conditions


def test_ingredients_reach_past_the_hand_picked_list(combined: DrugLexicon) -> None:
    # dupilumab is in the generated FDA ingredient list and not in the curated 250, so this
    # fails if data/ingredients.txt stops being loaded.
    assert combined.correct("on dupil you mab for eczema") == "on dupilumab for eczema"


def test_brand_long_tail_is_not_loaded(combined: DrugLexicon) -> None:
    # The FDA's 5228 brands are excluded on purpose: they corrupt ordinary speech (see
    # DEFAULT_LEXICONS). Forteo is the clearest case -- it captures a bare "for the".
    assert combined.correct("this is for the best") == "this is for the best"


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        # The transducer's characteristic failure: a long Latin/Greek compound split into
        # English-looking pieces. Same shape as the drug cases above.
        ("he has a trial fibrillation", "he has atrial fibrillation"),
        ("history of my cardial infarction", "history of myocardial infarction"),
        ("gastro esophageal reflux disease", "gastroesophageal reflux disease"),
        ("she has osteo arthritis in both knees", "she has osteoarthritis in both knees"),
        ("diagnosed with hypo thyroidism", "diagnosed with hypothyroidism"),
        ("he had a pulmonary em bolism", "he had a pulmonary embolism"),
        ("deep vein throm bosis", "deep vein thrombosis"),
        ("high per lipidemia", "hyperlipidemia"),
        ("thrombo cyto penia", "thrombocytopenia"),
        ("ulcer ative colitis", "ulcerative colitis"),
        ("an a philaxis", "anaphylaxis"),
        ("sleep ap nea", "sleep apnea"),
        # Lay pronunciations of eponyms, which the model spells as the English words.
        ("crones disease", "Crohn disease"),
        ("park in sons disease", "Parkinson disease"),
        ("all timers disease", "Alzheimer disease"),
    ],
)
def test_conditions_are_corrected(combined: DrugLexicon, heard: str, expected: str) -> None:
    assert combined.correct(heard) == expected


# Ordinary sentences with no medical content whatsoever. A correction here is a corrupted
# transcript, which is strictly worse than a missed one -- the user cannot tell it happened.
# "an email" is the case to beat: it folds to within one edit of "anemia" and is only
# rejected by the LENGTH_RATIO guard, so it is the canary for any threshold change.
@pytest.mark.parametrize(
    "sentence",
    [
        "I sent you an email this morning about the meeting",
        "an emergency came up so I had to leave early",
        "he is an amateur photographer on the weekends",
        "we need to address this issue before it grows",
        "the anniversary dinner is on Saturday night",
        "she said she would call me back later this afternoon",
        "I could not sleep last night because of the noise",
        "the article was published in a journal last year",
        "there is a lot of traffic on the highway right now",
        "the instructions were not very clear to me",
    ],
)
def test_ordinary_speech_is_untouched(combined: DrugLexicon, sentence: str) -> None:
    assert combined.correct(sentence) == sentence


# --- consonant-spine tier ----------------------------------------------------------
#
# Added after a real-audio run (scripts/medical_eval.py, macOS `say` through the live
# omi-med-stt server) lost enoxaparin -> "anoxoperin" and Ozempic -> "azampic" at ~0.72:
# vowel errors, which fold() preserves by design. Recovering them took 98.4% capture from
# 95.1% with no change to the false-positive count.


def test_skeleton_drops_vowels() -> None:
    assert skeleton("enoxaparin") == skeleton("anoxoperin")
    assert skeleton("Ozempic") == skeleton("azampic")
    assert skeleton("metformin") == "mtfrmn"


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("The nurse gave me anoxoperin injections", "The nurse gave me enoxaparin injections"),
        ("He started azampic last month", "He started Ozempic last month"),
    ],
)
def test_vowel_errors_recover_via_spine(combined: DrugLexicon, heard: str, expected: str) -> None:
    assert combined.correct(heard) == expected


def test_spine_never_fires_across_words(combined: DrugLexicon) -> None:
    # Measured multi-word spine collisions. Across a window the spine is coincidence, not
    # evidence, so the tier is single-word only.
    for phrase in ("my cousin", "the main", "almost an", "process in"):
        assert combined.correct(phrase) == phrase


def test_ambiguous_spine_is_refused_not_guessed() -> None:
    # citalopram and escitalopram share the spine "stlprm". Picking one would be a dosing
    # error waiting to happen, so the lexicon must leave the transcript alone instead.
    # "cetalaprom" folds to 0.700 against citalopram -- below any fuzzy threshold, so the
    # spine tier is the only thing that could fire here, which is what makes this a test of
    # the spine tier and not of the fuzzy one.
    assert DrugLexicon(["citalopram", "escitalopram"]).correct("cetalaprom") == "cetalaprom"
    # Drop the collision and the very same input resolves, proving the refusal above is the
    # ambiguity guard rather than the input simply being too far gone.
    assert DrugLexicon(["citalopram"]).correct("cetalaprom") == "citalopram"


def test_spine_respects_length(combined: DrugLexicon) -> None:
    # A spine must not match a drug of a very different length.
    assert combined.correct("mtf") == "mtf"


# --- non-word leniency -------------------------------------------------------------
#
# From the same real-audio runs: a UK voice lost Humira to "humera" (0.833) and prednisone
# to "pridnisome" (0.800), both just under SHORT_WINDOW_THRESHOLD. Neither is an English
# word, and the short-window rule exists to protect English. Skipped when /usr/share/dict/
# words is absent, since the fallback deliberately keeps the strict bar.

pytestmark_nonword = pytest.mark.skipif(
    not drug_lexicon_english(), reason="no /usr/share/dict/words on this machine"
)


@pytestmark_nonword
@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("The doctor mentioned humera as an option", "The doctor mentioned Humira as an option"),
        ("The chart says pridnisome", "The chart says prednisone"),
    ],
)
def test_nonword_single_tokens_get_the_plain_threshold(
    combined: DrugLexicon, heard: str, expected: str
) -> None:
    assert combined.correct(heard) == expected


@pytestmark_nonword
def test_real_english_words_keep_the_strict_threshold(combined: DrugLexicon) -> None:
    # These are the words that caused measured false positives with a bigger lexicon. Each
    # is in the wordlist, so it stays on the strict bar however close it scores.
    for word in ("violin", "machine", "invoice", "entrance", "surprise", "cousin"):
        assert combined.correct(word) == word


def test_nonword_check_is_conservative_when_unsure() -> None:
    # Multi-word windows and anything non-alphabetic are treated as English: the leniency
    # only ever applies to a single bare token.
    assert not DrugLexicon._is_nonword("humera", 2)
    assert not DrugLexicon._is_nonword("humera's", 1)
    assert not DrugLexicon._is_nonword("", 1)
