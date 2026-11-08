"""One patient turn: redact -> risk gate -> grounded reply or hand off.

The ordering is the safety property. When escalation is required, the
advice-generating LLM call is never made, so there is no advice to leak.
When it is made, the model may only use the clinic's own sources, and
grounding.check_reply drops anything the sources don't support. No sources,
no call: "I don't know" beats a fluent guess.
"""

import json
from dataclasses import dataclass, field

from app.config import DEFAULT_CLINIC, ClinicConfig
from app.grounding import check_reply, verify_claims
from app.knowledge import Chunk
from app.llm import LLMUnavailable, SafeLLM
from app.logs import log_event
from app.redaction import RedactionError
from app.risk import Risk, RiskAssessment, assess

# Tuned against evals/risk_cases.jsonl (scripts/eval_risk.py). Without level
# definitions the model sent every mention of a condition or medicine to a nurse.
RISK_SYSTEM = (
    "You assess the clinical risk of ONE message sent to a fertility clinic's assistant, for triage. "
    "You never give advice. Identifiers are replaced with [REDACTED]. The message may be in any language.\n"
    "Levels:\n"
    "- high: could be an emergency now. Chest pain or pressure, trouble breathing, heavy bleeding, fainting, "
    "severe or one-sided abdominal or pelvic pain (especially if pregnant, period late, or after a procedure), "
    "signs of stroke or a severe allergic reaction, thoughts of self-harm or suicide, possible overdose.\n"
    "- med: a new, worsening or unexplained symptom a nurse should assess soon but that is not clearly an "
    "emergency. For example fever, dizziness, a new lump, bleeding, spotting or bloating during or after "
    "fertility treatment, injection-site reactions, missed treatment doses, low mood or distress.\n"
    "- low: everything else. Questions about services, costs, hours or procedures; general education; and "
    "background explaining why they are seeking care, such as long-standing irregular or painful periods, "
    "trying to conceive, their age, a known condition like PCOS, medicines they take or have stopped, or past "
    "history. Mentioning a condition or a medicine is not by itself a risk.\n"
    "If the message describes a symptom and you are unsure between two levels, choose the higher.\n"
    'Reply with JSON only: {"risk_level": "low|med|high", "reason": "<10 words>", "confidence": "low|med|high"}'
)

REPLY_SYSTEM = (
    "You are Nightingale AI, an assistant for {clinic}. You are not a doctor or nurse. "
    "Be warm and brief: 2 to 4 sentences. Never diagnose, never suggest starting, stopping or changing "
    "medication, never give a treatment plan. Identifiers are replaced with [REDACTED]; never ask the "
    "patient to repeat them.\n"
    "Use ONLY the SOURCES below. Put the marker of the source you used, like [S1], at the end of every "
    "sentence that states a fact. Copy numbers exactly as the sources give them. If the sources don't "
    "answer the question, say you don't have that information and suggest asking the clinic team. "
    "Never add services, prices, times or facts that aren't in the sources.\n\n"
    "SOURCES:\n{sources}"
)


@dataclass
class TurnResult:
    reply: str
    risk: RiskAssessment
    show_send_to_clinic: bool
    advice_generated: bool
    citations: list[tuple[str, Chunk]] = field(default_factory=list)
    reply_confidence: str = "high"   # fixed templates are fully trusted


def format_sources(sources: list[Chunk]) -> str:
    return "\n\n".join(f"[S{n}] ({c.title} — {c.heading})\n{c.text}" for n, c in enumerate(sources, 1))


def not_covered_reply(clinic: ClinicConfig) -> str:
    return (f"I don't have clinic-approved information on that, so I'd rather not guess. The {clinic.name} "
            f"team can answer it: continue securely and a nurse will reply, usually within {clinic.response_window}.")


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


def trust_reply(clinic: ClinicConfig) -> str:
    """Fixed wording, never generated: what the AI is, what the clinic is, when a human steps in."""
    return (
        f"No, I'm not a doctor or a nurse. I'm Nightingale, an AI assistant that {clinic.name}, "
        f"{clinic.description}, uses to answer general questions and help you get your concern to their team. "
        f"I can't diagnose, prescribe, or change your treatment. "
        f"A real person gets involved when you tap Send to Clinic, or straight away if something you describe "
        f"sounds urgent: a nurse at {clinic.name} reads your conversation and replies, usually within "
        f"{clinic.response_window}. In an emergency, call {clinic.emergency_number}."
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


def with_history(text: str, history: list[tuple[str, str]] | None) -> str:
    """Recent turns (already redacted) so "how much does it cost?" knows what "it" is."""
    if not history:
        return text
    lines = "\n".join(f"{sender}: {content}" for sender, content in history)
    return f"CONVERSATION SO FAR:\n{lines}\n\nLATEST MESSAGE (answer this): {text}"


def handle_turn(text: str, llm: SafeLLM, clinic: ClinicConfig = DEFAULT_CLINIC,
                known_identifiers: list[str] | None = None, sources: list[Chunk] | None = None,
                history: list[tuple[str, str]] | None = None) -> TurnResult:
    # Rules run on the raw text locally: nothing leaves the server for this step
    try:
        risk = assess(text, llm_opinion=_llm_risk_opinion(llm, text))
    except (LLMUnavailable, RedactionError):
        risk = assess(text, llm_unavailable=True)

    log_event("risk_assessed", risk_level=risk.risk_level.label, source=risk.source,
              rule_codes=risk.rule_codes, intents=risk.intents)

    if risk.escalation_required:
        return TurnResult(safety_reply(risk, clinic), risk, show_send_to_clinic=True, advice_generated=False)
    if "identity_question" in risk.intents:
        return TurnResult(trust_reply(clinic), risk, show_send_to_clinic=True, advice_generated=False)

    if not sources:
        # Nothing in the knowledge base covers it: don't ask the model to improvise
        log_event("reply_not_covered", reason="no_sources")
        return TurnResult(not_covered_reply(clinic), risk, show_send_to_clinic=True, advice_generated=False)

    try:
        # Risk is judged on the latest message alone; only the reply sees the history
        reply = llm.complete(REPLY_SYSTEM.format(clinic=clinic.name, sources=format_sources(sources)),
                             with_history(text, history), purpose="reply", known_identifiers=known_identifiers)
    except (LLMUnavailable, RedactionError):
        reply = (f"Sorry, I can't answer properly right now. You can send this to {clinic.name} "
                 f"and a nurse will reply within {clinic.response_window}.")
        return TurnResult(reply, risk, show_send_to_clinic=True, advice_generated=False)

    checked = check_reply(reply, sources, patient_text=with_history(text, history))
    checked = verify_claims(checked, sources, llm, patient_text=with_history(text, history),
                            known_identifiers=known_identifiers)
    log_event("reply_grounding", grounded=checked.grounded, dropped=checked.dropped,
              citations=len(checked.citations), verified=checked.verified)
    if not checked.grounded:
        return TurnResult(not_covered_reply(clinic), risk, show_send_to_clinic=True, advice_generated=False,
                          reply_confidence="low")
    return TurnResult(checked.text, risk, show_send_to_clinic=risk.escalation_offered, advice_generated=True,
                      citations=checked.citations, reply_confidence=checked.confidence)
