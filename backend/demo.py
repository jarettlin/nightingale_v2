"""Try one patient turn by hand, without a real LLM.

    .venv/bin/python demo.py                     # interactive
    .venv/bin/python demo.py "I have crushing chest pain"
    .venv/bin/python demo.py --llm-down "what are your hours?"

The stand-in model says "low risk" to every risk question and echoes what it
received, so you can see exactly what would leave the server.
"""

import json
import logging
import sys
from itertools import count

from app.extraction import rule_extract
from app.intake import RISK_SYSTEM, handle_turn
from app.llm import LLMUnavailable, SafeLLM
from app.memory import InMemoryStore, apply_operations, profile


class EchoLLM:
    def __init__(self, down: bool = False):
        self.down = down

    def complete(self, system: str, user: str, timeout_s: float = 10.0) -> str:
        if self.down:
            raise LLMUnavailable("simulated outage")
        print(f"  → model received: {user!r}")
        if system == RISK_SYSTEM:
            return json.dumps({"risk_level": "low", "reason": "stand-in model", "confidence": "med"})
        return "(a real model would answer here with general, cited information)"


STORE = InMemoryStore()  # one patient's memory for the whole demo run
TURNS = count(1)


def show(text: str, down: bool) -> None:
    message_id = f"msg_{next(TURNS)}"
    print(f"\npatient ({message_id}): {text!r}")
    result = handle_turn(text, SafeLLM(EchoLLM(down)))
    r = result.risk
    print(f"  risk: {r.risk_level.label}  reason: {r.risk_reason}")
    print(f"  confidence: {r.confidence}  source: {r.source}  intents: {r.intents or '-'}")
    print(f"  send-to-clinic button: {result.show_send_to_clinic}  advice generated: {result.advice_generated}")
    print(f"  nightingale: {result.reply}")

    # Memory uses the offline rule extractor here (the echo model returns no operations)
    extraction = rule_extract(text, STORE.current())
    apply_operations(STORE, extraction.operations, message_id, extracted_by="rules")
    if extraction.clarify:
        print(f"  nightingale asks: {extraction.clarify}")
    print("  profile:")
    for kind, items in profile(STORE).items():
        for i in items:
            extra = f" {i['detail']}" if i["detail"] else ""
            flag = "  ⚠ conflicts with another entry" if i["conflict_with_id"] else ""
            print(f"    {kind}: {i['value']} ({i['status']}){extra}  ← {i['provenance_message_id']}, "
                  f"v{i['versions']}{flag}")


def main() -> None:
    args = sys.argv[1:]
    down = "--llm-down" in args
    args = [a for a in args if a != "--llm-down"]
    logging.basicConfig(level=logging.INFO, format="  log: %(message)s")

    if args:
        show(" ".join(args), down)
        return
    print("Type a patient message (Ctrl+D to quit).")
    for line in sys.stdin:
        if line.strip():
            show(line.strip(), down)


if __name__ == "__main__":
    main()
