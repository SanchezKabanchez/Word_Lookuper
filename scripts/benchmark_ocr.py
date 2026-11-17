#!/usr/bin/env python3
"""Compare bundled RapidOCR 1.4.4 models on local synthetic 1080p/1440p frames.

Run with the Python used to install requirements-ocr.txt:
    python scripts/benchmark_ocr.py --output artifacts/ocr-benchmark.json

Each mode gets its own process. Initialization and warmup are reported separately
from repeated inference. This measures OCR in isolation, not game FPS or Windows
hook/overlay latency; inference uses the package's bundled local models.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time


def _fixture(width, height):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (width, height), "#202b39")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, height // 2, width, height), fill="#17232d")
    font_paths = (Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf",
                  Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    font_path = next((path for path in font_paths if path.exists()), None)
    font_size = round(32 * height / 1080)
    font = (ImageFont.truetype(str(font_path), font_size) if font_path else
            ImageFont.load_default(size=font_size))
    lines = (
        "MISSION: THE LAST TRANSMISSION",
        "Follow the changing signal to the hidden passage.",
        "We will meet at the station before sunrise.",
        "Find the key and open the northern gate.",
        "That is an understatement.",
        "Press Enter to continue.",
    )
    for row, text in enumerate(lines):
        x = round(100 * width / 1920)
        y = round((150 + row * 125) * height / 1080)
        draw.text((x, y), text, fill="#f4f1e8", font=font)
    return image, str(font_path) if font_path else "Pillow default"


def _session_metadata(engine):
    sessions = (("detector", engine.text_det.infer.session),
                ("classifier", engine.text_cls.infer.session),
                ("recognizer", engine.text_rec.session.session))
    result = {}
    for name, session in sessions:
        options = session.get_session_options()
        entry = {"intra_op_num_threads": options.intra_op_num_threads,
                 "inter_op_num_threads": options.inter_op_num_threads,
                 "providers": session.get_providers()}
        for key in ("session.intra_op.allow_spinning", "session.inter_op.allow_spinning"):
            try:
                entry[key] = options.get_session_config_entry(key)
            except RuntimeError:
                entry[key] = "unset (ONNX Runtime default)"
        result[name] = entry
    return result


def _environment():
    result = {"platform": platform.platform(), "python": platform.python_version(),
              "logical_cpu_count": os.cpu_count(), "packages": {}}
    for package in ("rapidocr-onnxruntime", "onnxruntime", "opencv-python", "numpy", "pillow", "psutil"):
        result["packages"][package] = importlib.metadata.version(package)
    import psutil
    if hasattr(psutil.Process(), "cpu_affinity"):
        result["cpu_affinity_count"] = len(psutil.Process().cpu_affinity())
    quota = Path("/sys/fs/cgroup/cpu.max")
    if quota.exists():
        result["linux_cgroup_cpu_max"] = quota.read_text().strip()
    return result


def _worker(mode, repetitions, max_side):
    import cv2
    import numpy as np
    import psutil
    from rapidocr_onnxruntime import RapidOCR

    process = psutil.Process()
    record = {"mode": mode, "threads_before_engine": process.num_threads(),
              "opencv_threads_before": cv2.getNumThreads(), "max_input_side": max_side or None}
    parameters = {} if mode == "default" else {"intra_op_num_threads": 2, "inter_op_num_threads": 1}
    record["constructor_parameters"] = parameters
    started, cpu_started = time.perf_counter(), time.process_time()
    if mode == "application":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from word_lookup_ocr_budget import create_rapid_engine
        max_side = max_side or 1600
        engine = create_rapid_engine(threads=2, limit=max_side)
        # Match word_lookup_ocr._speed_up_rapid without importing the GUI runtime.
        engine.text_det.limit_type = "max"
        record["max_input_side"] = max_side
    else:
        engine = RapidOCR(**parameters)
    record["initialization"] = {"wall_seconds": time.perf_counter() - started,
                                "cpu_seconds": time.process_time() - cpu_started,
                                "threads_after_engine": process.num_threads()}
    record["sessions"] = _session_metadata(engine)
    record["opencv_threads"] = cv2.getNumThreads()
    record["detector_limit_type"] = engine.text_det.limit_type
    record["frames"] = []
    for width, height in ((1920, 1080), (2560, 1440)):
        image, font = _fixture(width, height)
        if mode == "application":
            from word_lookup_ocr_budget import fit_image
            image, _scale_x, _scale_y = fit_image(image, {"max_side": max_side}, upscale=1.0)
        elif max_side and max(image.size) > max_side:
            from PIL import Image
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        array = np.ascontiguousarray(np.array(image.convert("RGB"))[:, :, ::-1])
        started, cpu_started = time.perf_counter(), time.process_time()
        engine(array, use_cls=False, text_score=0.3, box_thresh=0.3)
        warmup = {"wall_seconds": time.perf_counter() - started,
                  "cpu_seconds": time.process_time() - cpu_started}
        samples = []
        texts = []
        for _ in range(repetitions):
            started, cpu_started = time.perf_counter(), time.process_time()
            result, _timings = engine(array, use_cls=False, text_score=0.3, box_thresh=0.3)
            samples.append({"wall_seconds": time.perf_counter() - started,
                            "cpu_seconds": time.process_time() - cpu_started,
                            "observed_process_threads": process.num_threads()})
            texts = [str(line[1]) for line in (result or [])]
        idle_started, cpu_started = time.perf_counter(), time.process_time()
        time.sleep(1.0)
        idle = {"wall_seconds": time.perf_counter() - idle_started,
                "cpu_seconds": time.process_time() - cpu_started}
        record["frames"].append({
            "source_size": [width, height], "ocr_input_size": list(image.size), "fixture_font": font,
            "warmup": warmup, "samples": samples,
            "median_wall_seconds": statistics.median(s["wall_seconds"] for s in samples),
            "median_cpu_seconds": statistics.median(s["cpu_seconds"] for s in samples),
            "idle_after_inference": idle, "recognized_text": texts,
            "recognized_text_sha256": hashlib.sha256("\n".join(texts).encode()).hexdigest(),
        })
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/ocr-benchmark.json"))
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--max-side", type=int, default=0,
                        help="Optional identical input-size cap for both modes; 0 keeps full frames")
    parser.add_argument("--mode", choices=("default", "limited", "application", "both", "all"), default="all")
    parser.add_argument("--worker-report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 30:
        parser.error("--repetitions must be between 1 and 30")
    if args.max_side and args.max_side < 320:
        parser.error("--max-side must be 0 or at least 320")
    if args.worker_report:
        if args.mode in ("both", "all"):
            parser.error("worker requires a single mode")
        args.worker_report.write_text(json.dumps(_worker(args.mode, args.repetitions, args.max_side), indent=2), encoding="utf-8")
        return

    report = {"environment": _environment(), "repetitions": args.repetitions,
              "limitations": ["Synthetic fixed text, not a running game or FPS measurement.",
                              "Default/limited processes use identical images and OCR thresholds; initialization and warmup are excluded from medians.",
                              "Limited mode changes only supported RapidOCR constructor thread budgets (2 intra-op, 1 inter-op); spinning retains ONNX defaults.",
                              "Application mode uses the production factory: bounded ONNX/OpenCV threads, no idle spin, detector max-side mode and capped input.",
                              "Image generation/conversion/resizing are excluded from timed inference; other host workloads are not controlled.",
                              "Observed total process threads include native image/math libraries; this is not a CPU-percent limit."],
              "results": []}
    modes = (("default", "limited", "application") if args.mode == "all" else
             ("default", "limited") if args.mode == "both" else (args.mode,))
    with tempfile.TemporaryDirectory(prefix="word-lookup-ocr-bench-") as temp:
        for mode in modes:
            worker_report = Path(temp) / f"{mode}.json"
            command = [sys.executable, str(Path(__file__).resolve()), "--mode", mode,
                       "--repetitions", str(args.repetitions), "--max-side", str(args.max_side),
                       "--worker-report", str(worker_report)]
            finished = subprocess.run(command, capture_output=True, text=True, timeout=180)
            if finished.returncode:
                raise SystemExit(f"{mode} benchmark failed:\n{finished.stderr[-3000:]}")
            report["results"].append(json.loads(worker_report.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("mode       frame         wall median   CPU median   observed threads")
    for result in report["results"]:
        for frame in result["frames"]:
            size = "x".join(map(str, frame["source_size"]))
            threads = max(s["observed_process_threads"] for s in frame["samples"])
            print(f"{result['mode']:<10} {size:<13} {frame['median_wall_seconds']:>9.3f}s {frame['median_cpu_seconds']:>11.3f}s {threads:>17}")
    print(f"Full report: {args.output.resolve()}")


if __name__ == "__main__":
    main()
