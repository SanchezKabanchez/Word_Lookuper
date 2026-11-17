import os
import subprocess
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

import word_lookup as w
import word_lookup_ocr as ocr
import word_lookup_ocr_budget as budget
from wl_network import LookupCancelled, network_scope


def test_native_ocr_gate_serializes_and_cancels_queued_work():
    entered = threading.Event()
    release = threading.Event()
    queued = threading.Event()
    cancelled = threading.Event()
    finished = threading.Event()
    executed = []

    def first():
        with budget.ocr_slot():
            entered.set()
            assert release.wait(2)

    def second():
        with network_scope(cancelled.is_set):
            queued.set()
            try:
                with budget.ocr_slot():
                    executed.append(True)
            except LookupCancelled:
                finished.set()

    owner = threading.Thread(target=first)
    waiting = threading.Thread(target=second)
    owner.start()
    assert entered.wait(1)
    waiting.start()
    try:
        assert queued.wait(1)
        cancelled.set()
        assert finished.wait(1), "An obsolete queued OCR job kept waiting"
        assert not executed
    finally:
        release.set()
        owner.join(2)
        waiting.join(2)
    assert not owner.is_alive() and not waiting.is_alive()


def test_large_frame_coordinates_use_actual_axis_scale(monkeypatch):
    w.cfg["ocr"]["max_side"] = 1600
    observed = []

    def engine(image, scale):
        observed.append((image.size, scale))
        return [w.W("word", 100, 120, 300, 160, 1)]

    monkeypatch.setattr(w, "ocr_tesseract", engine)
    words = w._run_ocr("tesseract", Image.new("RGB", (4001, 2251)), 4, False)
    assert observed == [((1600, 900), 1.0)]
    assert words[0].x0 == pytest.approx(100 * 4001 / 1600)
    assert words[0].y0 == pytest.approx(120 * 2251 / 900)
    assert words[0].x1 == pytest.approx(300 * 4001 / 1600)


def test_small_word_crop_keeps_useful_upscale(monkeypatch):
    observed = []
    monkeypatch.setattr(w, "ocr_tesseract", lambda image, scale: observed.append(image.size) or [])
    w._run_ocr("tesseract", Image.new("RGB", (80, 20)), 3, False)
    assert observed == [(240, 60)]


def test_wide_subtitle_band_is_tiled_without_shrinking_letters(monkeypatch):
    observed = []
    monkeypatch.setattr(w, "ocr_tesseract", lambda image, scale: observed.append(image.size) or [])
    w._run_ocr("tesseract", Image.new("RGB", (3840, 80)), 1, False)
    assert len(observed) == 3
    assert all(width <= 1600 and height == 80 for width, height in observed)


def test_tesseract_uses_private_thread_environment_timeout_and_tsv(monkeypatch):
    calls = []
    before = os.environ.get("OMP_THREAD_LIMIT")
    w.cfg["ocr"].update(cpu_threads=2, max_side=1600, tesseract_timeout=8)
    monkeypatch.setattr(w, "_configure_tesseract_cmd", lambda: SimpleNamespace(
        pytesseract=SimpleNamespace(tesseract_cmd="tesseract")))

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stderr=b"", stdout=(
            b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            b"5\t1\t1\t1\t1\t1\t100\t50\t200\t40\t95\tHello\n"))

    monkeypatch.setattr(w.subprocess, "run", run)
    words = w.ocr_tesseract(Image.new("RGB", (3200, 2000)), 1)
    assert calls[0][0] == ["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "6", "tsv"]
    assert calls[0][1]["env"]["OMP_THREAD_LIMIT"] == "2"
    assert calls[0][1]["timeout"] == 8
    assert calls[0][1]["input"].startswith(b"\x89PNG")
    assert os.environ.get("OMP_THREAD_LIMIT") == before
    assert words == [w.W("Hello", 200, 100, 600, 180, (1, 1, 1))]


def test_tesseract_timeout_is_bounded_and_reported(monkeypatch):
    monkeypatch.setattr(w, "_configure_tesseract_cmd", lambda: SimpleNamespace(
        pytesseract=SimpleNamespace(tesseract_cmd="tesseract")))
    monkeypatch.setattr(w.subprocess, "run", lambda args, **kwargs:
                        (_ for _ in ()).throw(subprocess.TimeoutExpired(args, kwargs["timeout"])))
    with pytest.raises(w.OcrError, match="тайм-аут"):
        w.ocr_tesseract(Image.new("RGB", (200, 50)), 1)


def test_failed_rapid_construction_restores_session_factory():
    class Session:
        @staticmethod
        def _init_sess_opts(config):
            return SimpleNamespace(add_session_config_entry=lambda key, value: entries.append((key, value)))

    entries = []
    original = Session.__dict__["_init_sess_opts"]
    with pytest.raises(RuntimeError):
        with budget._quiet_onnx_sessions(Session):
            Session._init_sess_opts({})
            raise RuntimeError("model could not load")
    assert Session.__dict__["_init_sess_opts"] is original
    assert entries == [("session.intra_op.allow_spinning", "0"),
                       ("session.inter_op.allow_spinning", "0")]


def test_rapid_cpu_setting_rebuilds_engine_for_next_call(monkeypatch):
    settings = []
    monkeypatch.setattr(w, "_rapid_run", None)
    monkeypatch.setattr(w._ocr, "rapid_settings", None)
    monkeypatch.setattr(w, "_speed_up_rapid", lambda engine: None)

    def construct(threads, limit):
        settings.append((threads, limit))
        return lambda *args, **kwargs: ([], [])

    monkeypatch.setattr(ocr, "create_rapid_engine", construct)
    w.cfg["ocr"].update(cpu_threads=2, max_side=1600)
    first = w._rapid_engine()
    assert w._rapid_engine() is first
    w.cfg["ocr"]["cpu_threads"] = 1
    assert w._rapid_engine() is not first
    assert settings == [(2, 1600), (1, 1600)]


def test_frame_words_supply_sentence_without_second_ocr(monkeypatch):
    words = (w.W("Hello", -400, 50, -350, 75, 0),
             w.W("wonderful", -340, 50, -230, 75, 0),
             w.W("world.", -220, 50, -150, 75, 0))
    hit = {"word": "wonderful", "box": (-340, 50, -230, 75), "_frame_words": words}
    monkeypatch.setattr(w, "pick_engine", lambda: (_ for _ in ()).throw(AssertionError("Repeated OCR")))
    assert w.find_sentence(hit) == "Hello wonderful world."


def test_cancelled_attempt_does_not_run_fallback(monkeypatch):
    cancelled = threading.Event()
    calls = []
    monkeypatch.setattr(w, "pick_engine", lambda: "tesseract")
    monkeypatch.setattr(w, "_grab", lambda *args: (Image.new("RGB", (600, 100)), 0, 0, 600, 100, 1, 1))

    def recognize(*args):
        calls.append(True)
        cancelled.set()
        return []

    monkeypatch.setattr(w, "_run_ocr", recognize)
    with network_scope(cancelled.is_set), pytest.raises(LookupCancelled):
        w.ocr_region(0, 0, 600, 100)
    assert len(calls) == 1
