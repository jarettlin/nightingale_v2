"""HTTP API. Every rule about who may see what is enforced here, server-side.

Access control, in order:
  1. `principal`: a valid Supabase JWT or 401. Signing keys unreachable -> 503.
  2. Role: guest / patient / staff, looked up from our own tables, never from
     anything the client claims.
  3. Ownership: a session is yours only if your user id created it or it is
     linked to your patient record. Anything else is 404 (not 403), so callers
     can't probe which ids exist.
  4. Staff endpoints: staff role or 403, clinic-scoped, and patient content
     only with the patient's current data-sharing consent.
Supabase RLS repeats rules 3–4 inside the database for anyone who talks to
Supabase directly with a browser token.
"""

from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import AuthError, AuthUnavailable, Principal, TokenVerifier, resolve_principal
from app.config import ClinicConfig
from app.conversion import (ConsentChoices, ConversionError, SignupDetails, conversion_offer, convert,
                            current_consent, start_auth, update_consent)
from app.crypto import Cipher
from app.escalation import (CLINICAL_ROLES, EscalationError, NeedsAccount, NeedsConsent, move, respond,
                            review_packet, send_to_clinic)
from app.llm import SafeLLM
from app.memory import profile
from app.pipeline import SessionExpired, SessionNotFound, handle_patient_message
from app.ratelimit import RateLimited, RateLimiter
from app.repository import LeadSession, Repository, now

# ?source=… on an arrival link. Comment DMs, staff referrals and lead forms get
# their own entry points in the channels step.
SOURCES = {
    "instagram": "instagram_ad_click",
    "google": "google_ad_click",
    "website": "website_widget",
    "widget": "website_widget",
    "google_reviews": "google_reviews",
    "review": "google_reviews",
}


@dataclass
class Deps:
    repo_scope: Callable[[], AbstractContextManager[Repository]]  # one transaction per request
    cipher: Cipher
    llm: SafeLLM
    verifier: TokenVerifier
    clinic: ClinicConfig
    limiter: RateLimiter
    extractor_llm: SafeLLM | None = None
    cors_origins: tuple[str, ...] = ()


# ------------------------------------------------------------------ bodies

class CreateSession(BaseModel):
    source: str = "direct"
    campaign: str | None = Field(None, max_length=100)
    creative: str | None = Field(None, max_length=100)
    landing_page: str | None = Field(None, max_length=300)


class SendMessage(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class Convert(BaseModel):
    phone: str = Field(min_length=6, max_length=20)
    data_sharing: bool
    marketing: bool = False
    exclude_memory_ids: list[str] = []


class ConsentChange(BaseModel):
    data_sharing: bool | None = None
    marketing: bool | None = None


class Escalate(BaseModel):
    trigger_message_id: str | None = None


class StatusChange(BaseModel):
    status: str


class ClinicianReply(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    visible_to_patient: bool = True


# ------------------------------------------------------------------ app

def create_app(deps: Deps) -> FastAPI:
    app = FastAPI(title="Nightingale API", version="0.1.0")
    app.state.deps = deps
    if deps.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(deps.cors_origins),
                           allow_methods=["*"], allow_headers=["authorization", "content-type"])

    emergency = f"If this is an emergency, exit Nightingale and dial {deps.clinic.emergency_number} for Emergency Services."

    @app.exception_handler(RateLimited)
    def _rate_limited(_: Request, exc: RateLimited):
        return JSONResponse({"detail": "Too many messages. Please wait a moment.", "emergency": emergency},
                            status_code=429, headers={"Retry-After": str(exc.retry_after)})

    # -------------------------------------------------------------- dependencies

    def get_repo():
        with deps.repo_scope() as repo:
            yield repo

    def principal(request: Request, repo: Repository = Depends(get_repo)) -> Principal:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(401, "Sign in required")
        try:
            claims = deps.verifier.verify(header[7:])
        except AuthUnavailable:
            raise HTTPException(503, f"Sign-in is temporarily unavailable. {emergency}")
        except AuthError:
            raise HTTPException(401, "Invalid or expired session")
        return resolve_principal(claims, repo)

    def staff_only(who: Principal = Depends(principal)) -> Principal:
        if who.role != "staff":
            raise HTTPException(403, "Staff only")
        return who

    def owned_session(repo: Repository, who: Principal, session_id: str) -> LeadSession:
        session = repo.get_lead_session(session_id)
        mine = session is not None and (
            session.guest_auth_user_id == who.auth_user_id
            or (who.patient_id is not None and session.patient_id == who.patient_id))
        if not mine:
            raise HTTPException(404, "Not found")
        return session

    def thread(repo: Repository, sessions: list[LeadSession], include_internal: bool = False) -> list[dict]:
        rows = []
        for s in sessions:
            for m in repo.list_messages(s.id):
                item = {"id": m.id, "sender": m.sender, "text": deps.cipher.decrypt(m.content_encrypted),
                        "created_at": m.created_at, "lead_session_id": s.id, "sent_as": m.sent_as}
                if include_internal and m.sender == "patient":
                    item |= {"risk_level": m.risk_level, "risk_reason": m.risk_reason}
                rows.append(item)
        return sorted(rows, key=lambda r: r["created_at"])

    def consented(repo: Repository, patient_id: str) -> bool:
        rows = [c for c in repo.consents(patient_id) if c.kind == "data_sharing"]
        return bool(rows) and max(rows, key=lambda c: c.recorded_at).granted

    # -------------------------------------------------------------- public

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/clinic")
    def clinic():
        c = deps.clinic
        return {"name": c.name, "description": c.description, "emergency_number": c.emergency_number,
                "response_window": c.response_window, "emergency_notice": emergency}

    # -------------------------------------------------------------- guests and patients

    @app.post("/sessions", status_code=201)
    def create_session(body: CreateSession, who: Principal = Depends(principal),
                       repo: Repository = Depends(get_repo)):
        if who.role == "staff":
            raise HTTPException(403, "Staff can't start patient sessions")
        deps.limiter.check("session_create", who.auth_user_id)
        session = repo.create_lead_session(
            clinic_id=deps.clinic.clinic_id,
            source_channel=SOURCES.get(body.source.lower(), "direct"),
            identity_level="email" if who.patient_id else "anonymous",
            campaign_id=body.campaign, creative=body.creative, landing_page=body.landing_page,
            # Until they're a patient, the session belongs to their user id, signed in or not.
            # (Leaving it unset made sessions nobody could open.)
            guest_auth_user_id=None if who.patient_id else who.auth_user_id,
            patient_id=who.patient_id, converted_at=now() if who.patient_id else None)
        greeting = (f"Hi, I'm Nightingale, {deps.clinic.name}'s assistant. Ask me anything about our services, "
                    f"hours or what to expect. No sign-up needed.")
        return {"session": {"id": session.id, "source_channel": session.source_channel,
                            "campaign_id": session.campaign_id, "stage": session.stage},
                "greeting": greeting, "emergency_notice": emergency}

    @app.post("/sessions/{session_id}/messages")
    def post_message(session_id: str, body: SendMessage, who: Principal = Depends(principal),
                     repo: Repository = Depends(get_repo)):
        session = owned_session(repo, who, session_id)
        deps.limiter.check("patient_message" if who.patient_id else "guest_message", who.auth_user_id)
        known = [who.email] if who.email else None
        try:
            result = handle_patient_message(repo, deps.llm, deps.cipher, session.id, body.text,
                                            clinic=deps.clinic, extractor_llm=deps.extractor_llm,
                                            known_identifiers=known)
        except SessionNotFound:
            raise HTTPException(404, "Not found")
        except SessionExpired:
            raise HTTPException(410, "This conversation has expired. Start a new one any time.")
        session = repo.get_lead_session(session.id)
        return {
            "message_id": result.patient_message_id,
            "reply": {"id": result.assistant_message_id, "text": result.reply,
                      "citations": result.citations, "confidence": result.reply_confidence},
            "risk_level": result.risk.risk_level.label,
            "show_send_to_clinic": result.show_send_to_clinic,
            "conversion_offer": conversion_offer(session, value_delivered=result.advice_generated,
                                                 escalation_offered=result.show_send_to_clinic,
                                                 clinic=deps.clinic),
            "profile": result.profile,
            "emergency_notice": emergency,
        }

    @app.get("/sessions/{session_id}/messages")
    def get_messages(session_id: str, who: Principal = Depends(principal), repo: Repository = Depends(get_repo)):
        return {"messages": thread(repo, [owned_session(repo, who, session_id)])}

    @app.get("/sessions/{session_id}/profile")
    def get_profile(session_id: str, who: Principal = Depends(principal), repo: Repository = Depends(get_repo)):
        session = owned_session(repo, who, session_id)
        return {"profile": profile(repo.memory_store(session.id))}

    @app.post("/sessions/{session_id}/auth-started", status_code=204)
    def auth_started(session_id: str, who: Principal = Depends(principal), repo: Repository = Depends(get_repo)):
        start_auth(repo, owned_session(repo, who, session_id).id)

    @app.post("/sessions/{session_id}/convert")
    def convert_session(session_id: str, body: Convert, who: Principal = Depends(principal),
                        repo: Repository = Depends(get_repo)):
        session = owned_session(repo, who, session_id)
        if who.is_anonymous or not who.email:
            raise HTTPException(401, "Verify your email first")
        # Supabase only issues a non-anonymous session once the email is confirmed
        details = SignupDetails(auth_user_id=who.auth_user_id, email=who.email, email_verified_at=now(),
                                phone=body.phone)
        try:
            result = convert(repo, deps.cipher, session.id, details,
                             ConsentChoices(body.data_sharing, body.marketing, body.exclude_memory_ids),
                             clinic=deps.clinic)
        except ConversionError as exc:
            raise HTTPException(409, str(exc))
        return {"patient_id": result.patient_id, "created": result.created,
                "opening_message": result.opening_message, "profile": result.profile}

    @app.post("/sessions/{session_id}/escalations", status_code=201)
    def escalate(session_id: str, body: Escalate, who: Principal = Depends(principal),
                 repo: Repository = Depends(get_repo)):
        session = owned_session(repo, who, session_id)
        try:
            result = send_to_clinic(repo, deps.cipher, session.id, body.trigger_message_id, clinic=deps.clinic)
        except NeedsAccount:
            raise HTTPException(409, {"code": "needs_account",
                                      "message": conversion_offer(session, value_delivered=False,
                                                                  escalation_offered=True, clinic=deps.clinic)})
        except NeedsConsent:
            raise HTTPException(409, {"code": "needs_consent",
                                      "message": f"To send this, allow {deps.clinic.name} to see your conversation."})
        except EscalationError:
            raise HTTPException(404, "Not found")
        e = result.escalation
        return {"escalation_id": e.id, "status": e.status, "already_open": result.already_open,
                "response_due_at": e.response_due_at, "confirmation": result.confirmation}

    @app.get("/me/sessions")
    def my_sessions(who: Principal = Depends(principal), repo: Repository = Depends(get_repo)):
        """Find someone's conversations by who they are, not by what one browser remembers.
        The email link often opens in a different browser: it must land on the same
        conversation, not a new empty one."""
        if who.role == "staff":
            return {"sessions": []}
        sessions = sorted(repo.sessions_for_auth_user(who.auth_user_id), key=lambda s: s.landing_timestamp,
                          reverse=True)
        return {"sessions": [{"id": s.id, "source_channel": s.source_channel, "landing_timestamp": s.landing_timestamp,
                              "converted": s.patient_id is not None} for s in sessions]}

    @app.get("/me/consent")
    def get_consent(who: Principal = Depends(principal), repo: Repository = Depends(get_repo)):
        if not who.patient_id:
            raise HTTPException(404, "Not found")
        return current_consent(repo, who.patient_id)

    @app.post("/me/consent")
    def set_consent(body: ConsentChange, who: Principal = Depends(principal), repo: Repository = Depends(get_repo)):
        if not who.patient_id:
            raise HTTPException(404, "Not found")
        sessions = repo.sessions_for_patient(who.patient_id)
        latest = max(sessions, key=lambda s: s.landing_timestamp).id if sessions else None
        return update_consent(repo, who.patient_id, deps.clinic.clinic_id, latest,
                              data_sharing=body.data_sharing, marketing=body.marketing)

    @app.get("/me/escalations")
    def my_escalations(who: Principal = Depends(principal), repo: Repository = Depends(get_repo)):
        if not who.patient_id:
            return {"escalations": []}
        out = []
        for e in repo.escalations_for_patient(who.patient_id):
            replies = [{"text": deps.cipher.decrypt(r.body_encrypted), "created_at": r.created_at}
                       for r in repo.clinician_responses(e.id) if r.visible_to_patient]
            out.append({"id": e.id, "status": e.status, "created_at": e.created_at,
                        "response_due_at": e.response_due_at, "responses": replies})
        return {"escalations": out}

    # -------------------------------------------------------------- staff

    @app.get("/staff/me")
    def staff_me(who: Principal = Depends(staff_only)):
        # For the UI only (hide what a role can't do); every action is still checked server-side
        return {"role": who.staff.role, "clinical": who.staff.role in CLINICAL_ROLES}

    @app.get("/staff/escalations")
    def queue(who: Principal = Depends(staff_only), repo: Repository = Depends(get_repo)):
        return {"escalations": [
            {"id": e.id, "risk_level": e.risk_level, "reason": e.reason, "status": e.status,
             "created_at": e.created_at, "response_due_at": e.response_due_at,
             "summary": [b["text"] for b in e.triage_summary]}
            for e in repo.escalation_queue(who.staff.clinic_id)]}

    @app.get("/staff/escalations/{escalation_id}")
    def review(escalation_id: str, who: Principal = Depends(staff_only), repo: Repository = Depends(get_repo)):
        try:
            packet = review_packet(repo, deps.cipher, who.staff, escalation_id)
        except EscalationError:
            raise HTTPException(404, "Not found")
        return {"escalation": asdict(packet["escalation"]), "sources": packet["sources"],
                "responses": [{"author_id": r.author_id, "created_at": r.created_at,
                               "text": deps.cipher.decrypt(r.body_encrypted)} for r in packet["responses"]]}

    @app.post("/staff/escalations/{escalation_id}/status")
    def change_status(escalation_id: str, body: StatusChange, who: Principal = Depends(staff_only),
                      repo: Repository = Depends(get_repo)):
        try:
            e = move(repo, who.staff, escalation_id, body.status)
        except EscalationError as exc:
            raise HTTPException(404 if "not found" in str(exc) else 409, str(exc))
        return {"id": e.id, "status": e.status}

    @app.post("/staff/escalations/{escalation_id}/responses", status_code=201)
    def reply(escalation_id: str, body: ClinicianReply, who: Principal = Depends(staff_only),
              repo: Repository = Depends(get_repo)):
        try:
            r = respond(repo, deps.cipher, who.staff, escalation_id, body.body, body.visible_to_patient)
        except EscalationError as exc:
            status = 404 if "not found" in str(exc) else 403 if "only nurses" in str(exc) else 409
            raise HTTPException(status, str(exc))
        return {"id": r.id, "created_at": r.created_at}

    @app.get("/staff/patients")
    def patients(who: Principal = Depends(staff_only), repo: Repository = Depends(get_repo)):
        out = []
        for pid in repo.list_patients(who.staff.clinic_id):
            if not consented(repo, pid):
                continue
            sessions = repo.sessions_for_patient(pid)
            out.append({"patient_id": pid,
                        "first_touch": sessions[0].attribution() if sessions else None,
                        "profile": profile(repo.memory_store(sessions[0].id)) if sessions else {}})
        return {"patients": out}

    @app.get("/staff/patients/{patient_id}/messages")
    def patient_messages(patient_id: str, who: Principal = Depends(staff_only), repo: Repository = Depends(get_repo)):
        patient = repo.get_patient(patient_id)
        if patient is None or patient["clinic_id"] != who.staff.clinic_id or not consented(repo, patient_id):
            raise HTTPException(404, "Not found")
        repo.add_audit("staff", "patient_thread_viewed", actor_id=who.staff.id, target_table="patient",
                       target_id=patient_id)
        return {"messages": thread(repo, repo.sessions_for_patient(patient_id), include_internal=True)}

    return app
