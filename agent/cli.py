"""Command line entry point.

Usage (from dpi_project/):
    python -m agent.cli "What was the median lead time in 2024?"
    python -m agent.cli --model gemini-3.5-flash-lite "..."
    python -m agent.cli --quiet "..."      # answer only, no step log
"""

import argparse
import sys

from agent.agent import ask


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask DPIP Agent a question about DORA metrics.")
    parser.add_argument("question", help="Plain-English question, in quotes.")
    parser.add_argument("--model", help="Override GEMINI_MODEL for this run.")
    parser.add_argument("--quiet", action="store_true", help="Print only the final answer.")
    parser.add_argument(
        "--sticky-fallback",
        action="store_true",
        help="Once GEMINI_FALLBACK_MODEL is used, keep using it for the rest of this question.",
    )
    args = parser.parse_args()

    # Windows consoles default to cp1252; model answers may contain other characters.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    result = ask(args.question, model=args.model, verbose=not args.quiet, sticky_fallback=args.sticky_fallback)

    print()
    print(result.answer)
    if not args.quiet:
        print()
        print(f"(model: {result.model}, status: {result.status}, trace: {result.trace_path})")
    return 0 if result.status == "answered" else 1


if __name__ == "__main__":
    sys.exit(main())
