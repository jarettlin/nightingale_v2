"""Guest -> patient: the handoff where trust usually breaks.

    1. Offer it only once the guest has received value or shown clinical intent,
       never on landing.
    2. Ask only for what we don't already know (an identified lead's email is prefilled).
    3. On verified sign-in: find or create the patient (immutable internal ID),
       record contact points, record consent (data sharing and marketing
       separately, each with a policy version and timestamp), then link the
       lead session.
    4. Nothing is copied. Linking the session is the migration, so every fact
       still points at the exact message the guest typed, and attribution stays
       on the session.
    5. The first message after signup shows what we already know. It never asks
       the patient to repeat their concern.

`convert` is idempotent: re-running it for an already-linked session returns
the same patient and writes nothing. So an interrupted signup can simply be
retried, and in Supabase the whole thing runs in one transaction.
"""

from dataclasses import dataclass, field

from app.config import DEFAULT_CLINIC, ClinicConfig
from app.crypto import Cipher
from app.memory import apply_operations, profile
from app.redaction import redact
from app.repository import Consent, ContactPoint, LeadSession, Repository, StoredMessage, now

POLICY_VERSION = "2026-11-v1"


class ConversionError(Exception):
    pass


@dataclass
class SignupDetails:
    auth_user_id: str | None
    email: str
    email_verified_at: str | None
    phone: str
    phone_verified_at: str | None = None


@dataclass
class ConsentChoices:
    data_sharing: bool
    marketing: bool = False                      # a separate checkbox; never implied
    exclude_memory_ids: list[str] = field(default_factory=list)


@dataclass
class ConversionResult:
    patient_id: str
    created: bool
    already_converted: bool
    opening_message: str
    profile: dict


# --------------------------------------------------------- before signup

def conversion_offer(session: LeadSession, *, value_delivered: bool, escalation_offered: bool,
                     clinic: ClinicConfig = DEFAULT_CLINIC) -> str | None:
    """The trust transition. None means: don't interrupt them yet."""
    if session.patient_id:
        return None
    if escalation_offered:
        return (f"A nurse at {clinic.name} can look at this properly. Continue securely to send it: "
                f"it takes about 30 seconds, and everything you've told me comes with you.")
    if value_delivered:
        return (f"Want to keep this conversation and ask {clinic.name} directly? Continue securely: "
                f"you won't need to repeat anything.")
    return None


def fields_to_collect(repo: Repository, session: LeadSession) -> dict:
    """Never ask an identified lead for what they already gave us."""
    contact = repo.get_lead_contact(session.id)
    prefill = {}
    if contact and contact.volunteered_email:
        prefill["email"] = contact.volunteered_email   # still verified by code, just not retyped
    ask = [f for f in ("email", "phone") if f not in prefill]
    return {"ask": ask, "prefill": prefill}


def start_auth(repo: Repository, lead_session_id: str) -> None:
    repo.add_funnel_event(lead_session_id, "auth_started")
    repo.advance_stage(lead_session_id, "auth_started")


# --------------------------------------------------------- the handoff

def _resolve_patient(repo: Repository, clinic_id: str, signup: SignupDetails) -> tuple[str, bool]:
    if signup.auth_user_id and (pid := repo.find_patient_by_auth_user(signup.auth_user_id)):
        return pid, False
    if pid := repo.find_patient_by_email(clinic_id, signup.email):
        return pid, False                        # returning patient on a new device
    return repo.create_patient(clinic_id, signup.auth_user_id), True


def _summary(session: LeadSession, prof: dict) -> list[str]:
    parts = []
    for kind in ("chief_complaint", "goal", "symptom"):
        for item in prof.get(kind, []):
            if item["status"] == "active":
                onset = item["detail"].get("onset") or item["detail"].get("since")
                parts.append(f"{item['value']} ({onset})" if onset else item["value"])
    meds = [m["value"] for m in prof.get("medication", []) if m["status"] == "active"]
    if meds:
        parts.append("you take " + ", ".join(meds))
    if not parts and session.referral_topic:
        parts.append(f"what you discussed with our team: {session.referral_topic}")
    return parts


def opening_message(session: LeadSession, prof: dict, consented: bool, clinic: ClinicConfig) -> str:
    known = _summary(session, prof)
    if consented:
        shared = f"{clinic.name} can now see this conversation, so a nurse can pick it up without starting over."
    else:
        shared = (f"I haven't shared anything with {clinic.name}. You can change that any time "
                  f"if you'd like a nurse to see it.")
    if known:
        return (f"You're all set. I've kept everything you told me, so you won't need to repeat it: "
                f"{'; '.join(known)}. {shared}")
    return f"You're all set. {shared} What would you like help with?"


def _retract_excluded(repo, cipher, session, sent_as, exclude_ids) -> None:
    """The patient chose not to carry some facts over. Retracting (not deleting)
    keeps the record honest; a system message is the provenance for the choice."""
    store = repo.memory_store(session.id)
    current_ids = {i.id for i in store.current()}
    targets = [i for i in exclude_ids if i in current_ids]
    if not targets:
        return
    note = f"Patient chose not to share {len(targets)} item(s) at signup."
    system_msg = repo.add_message(StoredMessage(
        lead_session_id=session.id, sender="system", sent_as=sent_as,
        content_encrypted=cipher.encrypt(note), content_redacted=note))
    apply_operations(store, [{"op": "retract", "target_id": t} for t in targets], system_msg.id,
                     extracted_by="patient_edit")


def convert(repo: Repository, cipher: Cipher, lead_session_id: str, signup: SignupDetails,
            consent: ConsentChoices, clinic: ClinicConfig = DEFAULT_CLINIC) -> ConversionResult:
    session = repo.get_lead_session(lead_session_id)
    if session is None:
        raise ConversionError("unknown session")
    if session.purged_at:
        raise ConversionError("session expired")
    if not signup.email_verified_at:
        raise ConversionError("email must be verified before an account is created")
    if not signup.phone:
        raise ConversionError("a contact phone number is required")

    patient_id, created = _resolve_patient(repo, session.clinic_id, signup)

    if session.patient_id:
        if session.patient_id != patient_id:
            raise ConversionError("session already belongs to another patient")
        prof = profile(repo.memory_store(session.id))
        return ConversionResult(patient_id, False, True,
                                opening_message(session, prof, consent.data_sharing, clinic), prof)

    repo.upsert_contact_point(ContactPoint(patient_id, "email", signup.email, signup.email_verified_at))
    repo.upsert_contact_point(ContactPoint(patient_id, "phone", signup.phone, signup.phone_verified_at))
    lead_contact = repo.get_lead_contact(session.id)
    if lead_contact and lead_contact.social_platform and lead_contact.social_handle:
        repo.upsert_contact_point(ContactPoint(patient_id, lead_contact.social_platform, lead_contact.social_handle))

    recorded_at = now()
    for kind, granted in (("data_sharing", consent.data_sharing), ("marketing", consent.marketing)):
        repo.add_consent(Consent(clinic_id=session.clinic_id, kind=kind, granted=granted,
                                 policy_version=POLICY_VERSION, patient_id=patient_id,
                                 lead_session_id=session.id, recorded_at=recorded_at))

    repo.link_session_to_patient(session.id, patient_id)
    session = repo.get_lead_session(session.id)

    _retract_excluded(repo, cipher, session, "patient", consent.exclude_memory_ids)

    if consent.data_sharing:
        repo.add_funnel_event(session.id, "consented")
        repo.advance_stage(session.id, "consented")
    if created:
        repo.add_funnel_event(session.id, "patient_created")
    repo.advance_stage(session.id, "patient_created")

    prof = profile(repo.memory_store(session.id))
    message = opening_message(session, prof, consent.data_sharing, clinic)
    redacted = redact(message)
    repo.add_message(StoredMessage(
        lead_session_id=session.id, sender="assistant", sent_as="patient",
        content_encrypted=cipher.encrypt(message),
        content_redacted=redacted.redacted_text if redacted.ok else None, redaction_ok=redacted.ok,
        reply_confidence="high", advice_generated=False))

    repo.add_audit("patient", "session_converted", actor_id=signup.auth_user_id,
                   target_table="lead_session", target_id=session.id,
                   patient_created=created, data_sharing=consent.data_sharing,
                   marketing=consent.marketing, excluded=len(consent.exclude_memory_ids))

    return ConversionResult(patient_id, created, False, message, prof)
