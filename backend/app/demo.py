"""Synthetic demo data: a few patients whose conversations reached the clinic.

Used by scripts/demo_server.py (in-memory, for trying the staff console and
recording a demo). Everything here is invented. It runs through the real
pipeline, conversion and escalation code, so the data has real provenance,
risk fields and triage summaries, just with a scripted model instead of OpenAI.
"""

import json
import re

from app.conversion import ConsentChoices, SignupDetails, convert, start_auth
from app.crypto import Cipher
from app.escalation import Staff, send_to_clinic
from app.intake import RISK_SYSTEM
from app.llm import SafeLLM
from app.pipeline import handle_patient_message

VERIFIED = "2026-11-08T09:00:00+00:00"


class ScriptedLLM:
    """Low risk from the model (the rules still raise real symptoms), and replies
    that quote the first clinic source, cited, the way a well-behaved model would."""

    def complete(self, system: str, user: str, timeout_s: float = 10.0, json_mode: bool = False) -> str:
        if system == RISK_SYSTEM:
            return json.dumps({"risk_level": "low", "reason": "scripted", "confidence": "med"})
        source = re.search(r"\[S1\] \(.*?\)\n(.+?)(?:\n\n\[S|\Z)", system, re.S)
        if not source:
            return "I don't have that information."
        return re.split(r"(?<=[.!?])\s+", source.group(1).strip())[0] + " [S1]"


PATIENTS = [
    {
        "auth": "demo-patient-mei", "email": "mei.demo@example.com", "phone": "+60120000001",
        "session": {"source_channel": "instagram_ad_click", "identity_level": "anonymous",
                    "campaign_id": "ivf_over40", "creative": "reel_03"},
        "guest": ["how does egg freezing work?", "I'm 41 and I've had irregular periods for 6 months, I take metformin"],
        "after": ["I'm pregnant and bleeding heavily with cramps"],
    },
    {
        "auth": "demo-patient-aisha", "email": "aisha.demo@example.com", "phone": "+60120000002",
        "session": {"source_channel": "staff_referral", "identity_level": "staff_referred",
                    "referral_topic": "asked about egg freezing at today's visit"},
        "guest": ["what does egg freezing cost?"],
        "after": ["really bloated since the egg retrieval and I feel dizzy"],
    },
    {
        "auth": "demo-patient-priya", "email": "priya.demo@example.com", "phone": "+60120000003",
        "session": {"source_channel": "google_ad_click", "identity_level": "anonymous", "campaign_id": "amh_test"},
        "guest": ["what is an AMH test", "I'm allergic to penicillin"],
        "after": ["my AMH came back low, do I have a fertility problem?"],
    },
]

STAFF = {
    "demo-nurse": Staff("staff-nurse-demo", "clinic_demo", "nurse"),
    "demo-front-desk": Staff("staff-desk-demo", "clinic_demo", "staff"),
}


def seed(repo, cipher: Cipher, clinic_id: str = "clinic_demo") -> None:
    llm = SafeLLM(ScriptedLLM())
    for auth_user_id, staff in STAFF.items():
        repo.add_staff(auth_user_id, staff)
    for p in PATIENTS:
        session = repo.create_lead_session(clinic_id=clinic_id, guest_auth_user_id=p["auth"], **p["session"])
        for text in p["guest"]:
            handle_patient_message(repo, llm, cipher, session.id, text)
        start_auth(repo, session.id)
        convert(repo, cipher, session.id, SignupDetails(p["auth"], p["email"], VERIFIED, p["phone"]),
                ConsentChoices(data_sharing=True))
        for text in p["after"]:
            handle_patient_message(repo, llm, cipher, session.id, text)
        send_to_clinic(repo, cipher, session.id)
