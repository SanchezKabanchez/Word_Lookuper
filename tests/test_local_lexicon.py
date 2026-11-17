import hashlib

import word_lookup_lexicon as lexicon


def test_bundled_frequency_data_matches_licensed_upstream():
    # Keep source pin/attribution meaningful when refreshing the bundled data.
    data = lexicon._DATA_PATH.read_bytes()
    assert hashlib.sha256(data).hexdigest() == "5351ff405b1126ef555791dd4d9798a48e3e9a501a9fc481a9da957752cfb458"
    assert len(data.splitlines()) == 50000
    notice = (lexicon._DATA_PATH.parent / "README.md").read_text(encoding="utf-8")
    assert "CC-BY-SA-4.0" in notice
    assert "Hermit Dave" in notice
    assert (lexicon._DATA_PATH.parent / "LICENSE-CC-BY-SA-4.0.txt").exists()


def test_frequency_labels_do_not_claim_cefr():
    common = lexicon.lookup("the")
    rarer = lexicon.lookup("understatement")
    assert common["frequency_rank"] < rarer["frequency_rank"]
    assert common["frequency_count"] > rarer["frequency_count"]
    assert common["frequency_label"] == "Дуже часте"
    assert not any("cefr" in key or "level" in key for key in common)


def test_local_irregular_forms_and_case_normalization():
    assert lexicon.lookup("Went")["base_forms"] == ["go"]
    assert lexicon.base_forms("children") == ["child"]
    assert "run" in lexicon.base_forms("running")
    assert lexicon.IRREGULAR_FORMS["saw"] == "see"
    assert "saw" in lexicon.IRREGULAR_AMBIGUOUS


def test_offline_ocr_correction_is_conservative():
    assert lexicon.correct_ocr("changin") == "changing"
    assert lexicon.correct_ocr("Changin") == "Changing"
    assert lexicon.correct_ocr("CHANGIN") == "CHANGING"
    assert lexicon.correct_ocr("systern") == "system"
    assert lexicon.correct_ocr("understatemenl") == "understatement"
    assert lexicon.correct_ocr("form") is None
    assert lexicon.correct_ocr("changing") is None
    assert lexicon.correct_ocr("blorf") is None
    assert lexicon.correct_ocr("x" * 10000) is None


def test_ambiguous_ocr_candidates_require_manual_choice(monkeypatch):
    monkeypatch.setattr(lexicon, "_frequencies", lambda: {
        "changing": (100, 5000), "chancing": (101, 4500),
    })
    assert lexicon.correct_ocr("chaning") is None


def test_missing_local_data_keeps_irregular_forms(monkeypatch, tmp_path):
    monkeypatch.setattr(lexicon, "_DATA_PATH", tmp_path / "not-installed.txt")
    lexicon._frequencies.cache_clear()
    try:
        assert lexicon.lookup("the") == {}
        assert lexicon.lookup("went") == {"base_forms": ["go"]}
        assert lexicon.correct_ocr("changin") is None
    finally:
        lexicon._frequencies.cache_clear()
