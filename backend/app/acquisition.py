"""Getting people in: staff referral links, social-comment DMs and lead forms.

Each prepares a lead session ahead of time, with its attribution and topic, behind
a link /r/<token>. Only a hash of the token is stored. Whoever opens the link first
claims the session, and the conversation opens already knowing why they came
(channel_rules.yaml decides the wording).

What we deliberately don't keep:
  * comment text: it's public and often personal; we keep only the topic it was about
  * names typed into staff notes: referral topics go through redaction first
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from app.channels import opening
from app.config import ClinicConfig
from app.conversion import POLICY_VERSION
from app.crypto import Cipher
from app.extraction import GOALS
from app.logs import log_event
from app.redaction import redact
from app.repository import Consent, LeadContact, LeadSession, Repository, StoredMessage

PLATFORM_CHANNEL = {"instagram": "instagram_comment", "facebook": "facebook_comment", "tiktok": "tiktok_comment"}
ONE_DM_PER_HANDLE = timedelta(hours=24)   # one private reply per person per day, however many comments
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LinkError(Exception):
    """Unknown, expired or already-claimed link: always reported the same way."""


class IntakeError(Exception):
    pass


def new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(24)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _clean_topic(topic: str | None) -> str | None:
    if not topic or not topic.strip():
        return None
    if len(topic) > 200:
        raise IntakeError("topic is too long")
    result = redact(topic.strip())
    if not result.ok:
        raise IntakeError("topic could not be made safe")
    return result.redacted_text


def link_for(frontend_url: str, token: str) -> str:
    return f"{frontend_url.rstrip('/')}/r/{token}"


# ------------------------------------------------------------------ opening a conversation

def start_conversation(repo: Repository, cipher: Cipher, session: LeadSession, clinic: ClinicConfig,
                       now: datetime) -> tuple[StoredMessage, list[str]]:
    """Store the channel-appropriate greeting as the first message. Returns it and what not to ask again."""
    o = opening(session, clinic, now)
    message = repo.add_message(StoredMessage(
        lead_session_id=session.id, sender="assistant", sent_as="patient" if session.patient_id else "guest",
        content_encrypted=cipher.encrypt(o.greeting), content_redacted=o.greeting, reply_confidence="high",
        advice_generated=False))
    log_event("conversation_opened", lead_session_id=session.id, rule=o.rule_id, time_of_day=o.time_of_day)
    return message, o.skip


# ------------------------------------------------------------------ staff referral

def create_staff_referral(repo: Repository, staff, topic: str, clinic: ClinicConfig,
                          frontend_url: str) -> tuple[str, LeadSession]:
    """A care-team member turns a conversation at the clinic into a personal link."""
    clean = _clean_topic(topic)
    if not clean:
        raise IntakeError("a topic is required")
    token, token_hash = new_token()
    session = repo.create_lead_session(
        log_visit=False, clinic_id=clinic.clinic_id, source_channel="staff_referral", identity_level="staff_referred",
        referral_topic=clean, referred_by=staff.id, resume_token_hash=token_hash)
    repo.add_audit("staff", "referral_link_created", actor_id=staff.id, target_table="lead_session",
                   target_id=session.id)
    return link_for(frontend_url, token), session


def claim_link(repo: Repository, cipher: Cipher, token: str, auth_user_id: str, patient_id: str | None,
               clinic: ClinicConfig, now: datetime) -> LeadSession:
    session = repo.find_session_by_token_hash(hash_token(token))
    if session is None or session.purged_at or session.expires_at < now.isoformat():
        raise LinkError("link not valid")
    if patient_id:
        if session.patient_id not in (None, patient_id) or (session.guest_auth_user_id not in (None, auth_user_id)):
            raise LinkError("link not valid")
        if session.patient_id is None:
            repo.link_session_to_patient(session.id, patient_id)
    elif not repo.claim_session(session.id, auth_user_id):
        raise LinkError("link not valid")
    session = repo.get_lead_session(session.id)
    if not repo.list_messages(session.id):
        repo.add_funnel_event(session.id, "visitor")   # the visit is when they open it, not when it was made
        start_conversation(repo, cipher, session, clinic, now)
    return session


# ------------------------------------------------------------------ social comments

@dataclass
class Comment:
    platform: str          # instagram | facebook | tiktok
    comment_id: str
    handle: str
    media_id: str | None
    text: str = field(repr=False)


class ReplySender(Protocol):
    def send_private_reply(self, platform: str, comment_id: str, text: str) -> bool: ...


class OutboxSender:
    """Records private replies instead of sending them. Real sending needs a Meta app that has passed
    review with instagram_manage_comments / pages_messaging, plus a page token."""

    def __init__(self):
        self.outbox: list[dict] = []

    def send_private_reply(self, platform: str, comment_id: str, text: str) -> bool:
        self.outbox.append({"platform": platform, "comment_id": comment_id, "text": text})
        log_event("private_reply_queued", platform=platform, comment_id=comment_id)
        return True


def verify_meta_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    """Meta signs each delivery: X-Hub-Signature-256: sha256=<hmac of the raw body with the app secret>."""
    if not header or not header.startswith("sha256=") or not app_secret:
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def parse_comment_webhook(payload: dict) -> list[Comment]:
    """Instagram (`object: instagram`, field `comments`), Facebook Pages (`object: page`, field `feed`,
    item `comment`), and a TikTok-shaped equivalent (`object: tiktok`) for the simulation."""
    platform = {"instagram": "instagram", "page": "facebook", "tiktok": "tiktok"}.get(payload.get("object"))
    if platform is None:
        return []
    comments = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if change.get("field") == "comments" and platform in ("instagram", "tiktok"):
                handle = value.get("from", {}).get("username")
                if value.get("id") and handle:
                    comments.append(Comment(platform, value["id"], handle, value.get("media", {}).get("id"),
                                            value.get("text", "")))
            elif change.get("field") == "feed" and value.get("item") == "comment" and value.get("verb") == "add":
                handle = value.get("from", {}).get("name") or value.get("from", {}).get("id")
                if value.get("comment_id") and handle:
                    comments.append(Comment(platform, value["comment_id"], handle, value.get("post_id"),
                                            value.get("message", "")))
    return comments


def topic_from_comment(text: str) -> str | None:
    low = text.lower()
    return next((topic for phrase, topic in GOALS.items() if re.search(rf"\b{re.escape(phrase)}\b", low)), None)


def handle_comment(repo: Repository, comment: Comment, clinic: ClinicConfig, frontend_url: str,
                   sender: ReplySender, now: datetime) -> str:
    """Comment on a clinic post -> prepared session + one private reply with a link. Returns what happened."""
    if repo.recent_session_for_handle(comment.platform, comment.handle, (now - ONE_DM_PER_HANDLE).isoformat()):
        return "skipped_recent"   # never spam someone who comments twice
    token, token_hash = new_token()
    session = repo.create_lead_session(
        log_visit=False, clinic_id=clinic.clinic_id, source_channel=PLATFORM_CHANNEL[comment.platform],
        identity_level="handle", campaign_id=comment.media_id, referral_topic=topic_from_comment(comment.text),
        resume_token_hash=token_hash)
    repo.set_lead_contact(LeadContact(session.id, social_platform=comment.platform, social_handle=comment.handle))
    template = opening(session, clinic, now).private_reply
    if not template:
        return "no_reply_rule"
    sent = sender.send_private_reply(comment.platform, comment.comment_id, template.format(link=link_for(frontend_url, token)))
    repo.add_audit("system", "comment_private_reply", target_table="lead_session", target_id=session.id,
                   platform=comment.platform, sent=sent)
    return "replied" if sent else "send_failed"


# ------------------------------------------------------------------ lead forms

def handle_lead_form(repo: Repository, email: str, topic: str | None, marketing_consent: bool, form_id: str | None,
                     clinic: ClinicConfig, frontend_url: str) -> str:
    """An identified lead: they gave their email, so the conversation will never ask for it again."""
    if not EMAIL.match(email or ""):
        raise IntakeError("a valid email is required")
    token, token_hash = new_token()
    session = repo.create_lead_session(
        log_visit=False, clinic_id=clinic.clinic_id, source_channel="lead_form", identity_level="email",
        campaign_id=form_id, referral_topic=_clean_topic(topic), resume_token_hash=token_hash)
    repo.set_lead_contact(LeadContact(session.id, volunteered_email=email.strip()))
    if marketing_consent:  # a separate, explicit checkbox on the form, timestamped here
        repo.add_consent(Consent(clinic_id=clinic.clinic_id, kind="marketing", granted=True,
                                 policy_version=POLICY_VERSION, lead_session_id=session.id))
    repo.add_audit("system", "lead_form_received", target_table="lead_session", target_id=session.id,
                   marketing=marketing_consent)
    return link_for(frontend_url, token)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
