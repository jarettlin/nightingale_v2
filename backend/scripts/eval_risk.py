"""Score the risk gate against evals/risk_cases.jsonl.

    .venv/bin/python scripts/eval_risk.py              # rules + the configured OpenAI model
    .venv/bin/python scripts/eval_risk.py --rules-only # deterministic, no API calls
    .venv/bin/python scripts/eval_risk.py --runs 3     # repeat to see how stable the model is

What matters, in order:
  1. High recall: every "high" case must come out high. Missing one is a safety failure.
  2. Escalation: every "med" case must reach a clinician (med or high).
  3. Over-escalation: how many routine "low" cases were sent to a nurse instead of answered.
     Lower is better, but only ever traded against 1 and 2, never ahead of them.
"""

import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(Path(__file__).parent.parent / ".env")

from app.intake import _llm_risk_opinion  # noqa: E402
from app.llm import LLMUnavailable, OpenAIClient, SafeLLM  # noqa: E402
from app.risk import assess  # noqa: E402

CASES = Path(__file__).parent.parent / "evals" / "risk_cases.jsonl"


def load_cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]


def score(case: dict, llm: SafeLLM | None) -> str:
    if llm is None:
        return assess(case["text"]).risk_level.label
    try:
        return assess(case["text"], llm_opinion=_llm_risk_opinion(llm, case["text"])).risk_level.label
    except LLMUnavailable:
        return assess(case["text"], llm_unavailable=True).risk_level.label


def run(cases: list[dict], llm: SafeLLM | None) -> list[str]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda c: score(c, llm), cases))


def report(cases: list[dict], results: list[list[str]]) -> bool:
    runs = len(results)
    misses, confusion = [], Counter()
    for i, case in enumerate(cases):
        allowed = set(case.get("allow", [case["expect"]]))
        got = [r[i] for r in results]
        for g in got:
            confusion[(case["expect"], g)] += 1
        if any(g not in allowed for g in got):
            misses.append((case, got))

    def rate(expect: str, ok) -> str:
        total = sum(n for (e, _), n in confusion.items() if e == expect)
        good = sum(n for (e, g), n in confusion.items() if e == expect and ok(g))
        return f"{good}/{total}"

    print(f"\n{len(cases)} cases × {runs} run(s)\n")
    print("expected →   low   med  high")
    for e in ("low", "med", "high"):
        print(f"  {e:5}    " + "  ".join(f"{confusion[(e, g)]:4}" for g in ("low", "med", "high")))
    high_ok = rate("high", lambda g: g == "high")
    print(f"\nhigh recall (must be all):       {high_ok}")
    print(f"med reaches a clinician:         {rate('med', lambda g: g in ('med', 'high'))}")
    print(f"low answered (not sent to nurse): {rate('low', lambda g: g == 'low')}")
    if misses:
        print("\noutside the allowed levels:")
        for case, got in misses:
            print(f"  expect {case['expect']:4} got {','.join(got):14} {case['text']!r}  ({case['why']})")
    total_high = sum(n for (e, _), n in confusion.items() if e == "high")
    return confusion_high_ok(confusion, total_high)


def confusion_high_ok(confusion: Counter, total_high: int) -> bool:
    return confusion[("high", "high")] == total_high


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules-only", action="store_true")
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()

    cases = load_cases()
    llm = None
    if not args.rules_only:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            sys.exit("OPENAI_API_KEY not set (or use --rules-only)")
        model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
        print(f"model: {model}")
        llm = SafeLLM(OpenAIClient(key, model))
    else:
        print("rules only")

    results = [run(cases, llm) for _ in range(args.runs)]
    ok = report(cases, results)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
