import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

from PIL import Image
import pytest

import word_lookup as w
from word_lookup_diagnostics import DiagnosticsRecorder, environment_info


def test_local_package_redacts_keys_and_contains_only_opt_in_crop(tmp_path, app):
    config = copy.deepcopy(w.DEFAULT_CONFIG)
    config["diagnostics"].update(enabled=True, include_ocr_image=True)
    recorder = DiagnosticsRecorder(lambda: config, lambda text: text.replace("synthetic-secret", "***"))
    recorder.record("lookup.started", token=1)
    image = Image.new("RGB", (2000, 1000), "white")
    recorder.capture_ocr(image, [SimpleNamespace(text="sample", x0=20, y0=20, x1=130, y1=55)], "synthetic")
    package = tmp_path / "diagnostics.zip"
    recorder.export(str(package), "key: synthetic-secret", environment_info(app.root), {"count": 35})
    with zipfile.ZipFile(package) as archive:
        assert "synthetic-secret" not in archive.read("report.txt").decode()
        assert "ocr-boxes.png" in archive.namelist()
        assert "saved_words.json" not in archive.namelist()
        assert json.loads(archive.read("library-summary.json")) == {"count": 35}
        info = json.loads(archive.read("environment.json"))
        assert info["app_version"] == w.__version__ and "tk_scaling_pixels_per_point" in info["display"]
    config["diagnostics"]["include_ocr_image"] = False
    recorder.export(str(package), "report", {}, {})
    with zipfile.ZipFile(package) as archive:
        assert "ocr-boxes.png" not in archive.namelist()


def test_disabled_diagnostics_do_not_capture_or_sample(monkeypatch):
    recorder = DiagnosticsRecorder(lambda: w.DEFAULT_CONFIG, lambda text: text)
    recorder.record("ignored")
    recorder.capture_ocr(Image.new("RGB", (10, 10)), [], "synthetic")
    recorder.sample()
    assert recorder._crop is None and not recorder._events and recorder._process is None


def test_fps_summary_uses_real_csv_intervals_and_largest_swap_chain(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts" / "summarize_fps.py"
    spec = importlib.util.spec_from_file_location("fps_summary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    capture = tmp_path / "synthetic-presentmon.csv"
    capture.write_text("ProcessID,SwapChainAddress,msBetweenPresents\n"
                       "1,main,10\n1,main,10\n1,main,20\n1,secondary,100\n1,main,NA\n", encoding="utf-8")
    result = module.summarize(capture)
    assert result["frames"] == 3 and result["swap_chain"] == "main"
    assert result["average_fps"] == pytest.approx(75)
    assert result["p99_ms"] == 20 and result["low_1_percent_fps"] == 50
