"""Run the synthetic QLIE translation benchmark without a network model."""

from __future__ import annotations

import argparse
import sys

from nagi.evaluation import (
    evaluate_qlie_translation_benchmark,
    load_qlie_translation_benchmark,
    render_qlie_translation_evaluation_text,
    write_qlie_translation_artifact,
)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Evaluate the synthetic QLIE extraction-to-patch-dry-run pipeline."
    )
    parser.add_argument(
        "benchmark",
        nargs="?",
        default="benchmarks/qlie_translation_tasks.json",
        help="Versioned synthetic benchmark JSON.",
    )
    parser.add_argument("--output", help="Optional JSON artifact output path.")
    parser.add_argument("--retrieval-repeats", type=int, help="Override retrieval repeats.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON artifact.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        benchmark = load_qlie_translation_benchmark(args.benchmark)
        report = evaluate_qlie_translation_benchmark(
            benchmark,
            retrieval_repeats=args.retrieval_repeats,
        )
        if args.output:
            write_qlie_translation_artifact(report, args.output)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"QLIE translation benchmark failed: {exc}", file=sys.stderr)
        return 2
    print(report.to_json() if args.json else render_qlie_translation_evaluation_text(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
