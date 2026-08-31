"""Aggregate content-free metrics for the conservative QLIE parser."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from nagi.gameio.qlie import parse_exported_script
from nagi.gameio.qlie.script import _line_shape


def evaluate(export_dir):
    root = Path(export_dir).expanduser().resolve()
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    items = [
        item
        for item in manifest.get("items", [])
        if isinstance(item, dict) and item.get("status") == "exported"
    ]
    statuses = Counter()
    encodings = Counter()
    kinds = Counter()
    totals = Counter()
    unknown_shapes = Counter()
    warning_files = 0
    for item in items:
        result = parse_exported_script(root, item.get("output_path"))
        summary = result.to_dict()["summary"]
        statuses[result.status] += 1
        if result.encoding:
            encodings[result.encoding] += 1
        kinds.update(summary["kind_counts"])
        totals["lines"] += summary["line_count"]
        totals["nonblank_lines"] += summary["nonblank_line_count"]
        totals["segments"] += summary["segment_count"]
        totals["translatable"] += summary["translatable_count"]
        totals["unknown"] += summary["unknown_count"]
        warning_files += bool(result.warnings)
        for segment in result.segments:
            if segment.kind == "unknown":
                unknown_shapes[_line_shape(segment.source_text)] += 1
                totals["unknown_with_non_ascii"] += any(
                    ord(char) > 0x80 for char in segment.source_text
                )
    return {
        "schema_version": 1,
        "engine": "qlie",
        "status": "supported" if statuses == {"supported": len(items)} else "partial",
        "summary": {
            "file_count": len(items),
            "status_counts": dict(sorted(statuses.items())),
            "encoding_counts": dict(sorted(encodings.items())),
            "line_count": totals["lines"],
            "nonblank_line_count": totals["nonblank_lines"],
            "segment_count": totals["segments"],
            "translatable_count": totals["translatable"],
            "unknown_count": totals["unknown"],
            "unknown_with_non_ascii_count": totals["unknown_with_non_ascii"],
            "unknown_rate_nonblank": (
                totals["unknown"] / max(1, totals["nonblank_lines"])
            ),
            "warning_file_count": warning_files,
            "kind_counts": dict(sorted(kinds.items())),
            "top_unknown_shapes": [
                {"shape": shape, "count": count}
                for shape, count in sorted(
                    unknown_shapes.items(), key=lambda pair: (-pair[1], pair[0])
                )[:20]
            ],
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Evaluate one Phase 1 corpus without emitting script text."
    )
    parser.add_argument("export_dir", help="Phase 1 export directory.")
    args = parser.parse_args(argv)
    report = evaluate(args.export_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "supported" else 2


if __name__ == "__main__":
    raise SystemExit(main())
