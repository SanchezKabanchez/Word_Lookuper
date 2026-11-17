"""Summarize real PresentMon CSV captures; no simulated game FPS."""
import argparse
from collections import defaultdict
import csv
import json
import math
from pathlib import Path
import statistics


def summarize(path: Path) -> dict:
    groups = defaultdict(list)
    field = None
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        field = next((name for name in ("msBetweenPresents", "MsBetweenPresents", "FrameTime", "CPUFrameTime")
                      if name in (reader.fieldnames or [])), None)
        if field is None:
            raise ValueError("No frame-time column; capture with PresentMon --v1_metrics.")
        for row in reader:
            try:
                duration = float(row[field])
            except (TypeError, ValueError):
                continue
            if math.isfinite(duration) and duration > 0:
                groups[(row.get("ProcessID", ""), row.get("SwapChainAddress", ""))].append(duration)
    if not groups:
        raise ValueError("Capture contains no valid frame intervals.")
    group, values = max(groups.items(), key=lambda item: len(item[1]))
    ordered = sorted(values)
    def percentile(fraction):
        return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]
    slowest = ordered[-max(1, math.ceil(len(ordered) * .01)):]
    return {"file": path.name, "frame_time_column": field, "process_id": group[0],
            "swap_chain": group[1], "frames": len(values), "average_fps": 1000 / statistics.mean(values),
            "low_1_percent_fps": 1000 / statistics.mean(slowest), "p50_ms": percentile(.5),
            "p95_ms": percentile(.95), "p99_ms": percentile(.99)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps([summarize(path) for path in args.files], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
