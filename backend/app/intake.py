"""One patient turn: redact -> risk gate -> reply or hand off.

The ordering is the safety property. When escalation is required, the
advice-generating LLM call is never made, so there is no advice to leak.
Database writes and memory extraction plug in here later.
"""

import json
from dataclasses import dataclass

from app.config import DEFAULT_CLINIC, ClinicConfig
from app.llm import LLMUnavailable, SafeLLM
from app.logs import log_event
from app.redaction import RedactionError
from app.risk import Risk, RiskAssessment, assess

RISK_SYSTEM = (
    "You assess clinical risk of ONE patient message for triage. You never give advice. "
    "Identifiers are replaced with [REDACTED]. Reply with JSON only: "
    '{"risk_level": "low|med|high", "reason": "<10 words>", "confidence": "low|med|high"}. '
    "If unsure between two levels, choose the higher."
)

REPLY_SYSTEM = (
    "You are Nightingale AI, an assistant for {clinic}. You are not a doctor or nurse. "
    "Be warm and brief. Never diagnose, never suggest starting, stopping or changing medication, "
    "never give a treatment plan. Give general information only and suggest the clinic for anything "
    "personal. Identifiers are replaced with [REDACTED]; never ask the patient to repeat them."
)


@dataclass
class TurnResult:
    reply: str
    risk: RiskAssessment
    show_send_to_clinic: bool
    advice_generated: bool


def safety_reply(risk: RiskAssessment, clinic: ClinicConfig) -> str:
    if risk.risk_level == Risk.HIGH:
        # High risk is not a 12–18 hour problem: point to emergency care first
        return (
            f"I'm really glad you told me. What you're describing needs urgent attention, "
            f"so I'm not going to give advice here. Please call {clinic.emergency_number} now "
            f"or go to the nearest emergency department. If you can, tap Send to Clinic as well "
            f"so the {clinic.name} team has your details when you're safe."
        )
    if risk.risk_level == Risk.MED and not risk.rule_codes and risk.source == "rules+llm_unavailable":
        # Raised to Med only because we couldn't double-check: say so honestly
        return (
            f"Sorry, I can't answer reliably right now. You can send this to {clinic.name} and a nurse "
            f"will reply within {clinic.response_window}, or try me again in a few minutes. "
            f"If this is urgent, call {clinic.emergency_number}."
        )
    if risk.risk_level == Risk.MED:
        return (
            f"Thank you for sharing that. This is something a nurse at {clinic.name} should look at "
            f"rather than me, because I can't assess symptoms safely. Tap Send to Clinic and they'll "
            f"see exactly what you've told me, so you won't need to repeat it. They usually reply "
            f"within {clinic.response_window}. If it gets worse, call {clinic.emergency_number}."
        )
    # Low risk but asking for a diagnosis or a medication change
    return (
        f"That's a really reasonable question, and it's one for your care team rather than me: "
        f"I can't diagnose or advise on medication. Tap Send to Clinic and a nurse at {clinic.name} "
        f"will pick it up with everything you've said so far. They usually reply within "
        f"{clinic.response_window}."
    )


def _llm_risk_opinion(llm: SafeLLM, text: str) -> dict:
    raw = llm.complete(RISK_SYSTEM, text, purpose="risk")
    try:
        opinion = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMUnavailable("unparseable risk opinion") from exc
    if not isinstance(opinion, dict):
        raise LLMUnavailable("unparseable risk opinion")
    return opinion


def handle_turn(text: str, llm: SafeLLM, clinic: ClinicConfig = DEFAULT_CLINIC,
                known_identifiers: list[str] | None = None) -> TurnResult:
    # Rules run on the raw text locally: nothing leaves the server for this step
    try:
        risk = assess(text, llm_opinion=_llm_risk_opinion(llm, text))
    except (LLMUnavailable, RedactionError):
        risk = assess(text, llm_unavailable=True)

    log_event("risk_assessed", risk_level=risk.risk_level.label, source=risk.source,
              rule_codes=risk.rule_codes, intents=risk.intents)

    if risk.escalation_required:
        return TurnResult(safety_reply(risk, clinic), risk, show_send_to_clinic=True, advice_generated=False)

    try:
        reply = llm.complete(REPLY_SYSTEM.format(clinic=clinic.name), text, purpose="reply",
                             known_identifiers=known_identifiers)
    except (LLMUnavailable, RedactionError):
        reply = (f"Sorry, I can't answer properly right now. You can send this to {clinic.name} "
                 f"and a nurse will reply within {clinic.response_window}.")
        return TurnResult(reply, risk, show_send_to_clinic=True, advice_generated=False)

    return TurnResult(reply, risk, show_send_to_clinic=risk.escalation_offered, advice_generated=True)
