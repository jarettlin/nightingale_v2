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

from app.intake import RISK_SYSTEM, handle_turn
from app.llm import LLMUnavailable, SafeLLM


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


def show(text: str, down: bool) -> None:
    print(f"\npatient: {text!r}")
    result = handle_turn(text, SafeLLM(EchoLLM(down)))
    r = result.risk
    print(f"  risk: {r.risk_level.label}  reason: {r.risk_reason}")
    print(f"  confidence: {r.confidence}  source: {r.source}  intents: {r.intents or '-'}")
    print(f"  send-to-clinic button: {result.show_send_to_clinic}  advice generated: {result.advice_generated}")
    print(f"  nightingale: {result.reply}")


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
