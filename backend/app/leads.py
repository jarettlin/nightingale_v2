"""Conversion intelligence: funnel per channel, and warm leads.

Warm-lead score: four transparent parts, each shown to staff, no hidden weights.
  recency   0–40  halves every 24h since the person last did anything
  stage     0–25  how far through the funnel they got
  identity  0–20  how well we could reach them (none, handle, email, met in person)
  channel   0–10  how warm the channel tends to be (a staff referral beats an ad click)

Safety rule: a lead with a clinical concern (a medium/high-risk message, or an open
escalation) is a compassion priority, not a sales one. It's listed apart, routed
to the nurse queue, and never gets a follow-up suggestion.

Privacy: staff see what someone asked about (or any health detail) only after
consent. Before that, a lead shows only the topic they arrived with (the ad, the
referral note, the comment topic), never what they typed.
"""

from datetime import datetime, timedelta

from app.channels import topic_for
from app.repository import STAGES, Repository

FUNNEL = ["visitor", "conversation_started", "value_event", "auth_started", "consented", "patient_created",
          "escalation_sent"]
STAGE_POINTS = {"visitor": 0, "conversation_started": 5, "value_event": 12, "auth_started": 18, "consented": 22,
                "patient_created": 25, "escalation_sent": 25}
IDENTITY_POINTS = {"anonymous": 0, "handle": 8, "email": 15, "staff_referred": 20}
CHANNEL_POINTS = {"staff_referral": 10, "lead_form": 8, "instagram_comment": 6, "tiktok_comment": 6,
                  "facebook_comment": 6, "instagram_ad_click": 4, "google_ad_click": 4, "google_reviews": 4,
                  "website_widget": 3, "direct": 0}


def funnel(repo: Repository, clinic_id: str, since: datetime) -> dict:
    """Unique sessions reaching each step, per channel, and conversion from first visit."""
    reached: dict[str, dict[str, set]] = {}
    for row in repo.funnel_rows(clinic_id, since.isoformat()):
        if row["event"] in FUNNEL:
            reached.setdefault(row["source_channel"], {e: set() for e in FUNNEL})[row["event"]].add(row["lead_session_id"])
    channels = {}
    for channel, steps in sorted(reached.items()):
        counts = {e: len(s) for e, s in steps.items()}
        visitors = counts["visitor"] or 0
        channels[channel] = {
            "counts": counts,
            "rates": {e: (round(counts[e] / visitors, 2) if visitors else None) for e in FUNNEL},
        }
    return {"steps": FUNNEL, "channels": channels}


def _last_activity(repo: Repository, session) -> str:
    messages = repo.list_messages(session.id)
    return max([session.landing_timestamp] + [m.created_at for m in messages])


def _consent(repo: Repository, session) -> dict:
    rows = repo.consents(session.patient_id) if session.patient_id else []
    latest = {}
    for c in sorted(rows, key=lambda c: c.recorded_at):
        latest[c.kind] = c.granted
    return {"data_sharing": latest.get("data_sharing", False), "marketing": latest.get("marketing", False)}


def warm_leads(repo: Repository, clinic_id: str, now: datetime, days: int = 30) -> dict:
    leads, clinical = [], []
    for s in repo.lead_sessions(clinic_id, (now - timedelta(days=days)).isoformat()):
        last = _last_activity(repo, s)
        hours = max(0.0, (now - datetime.fromisoformat(last)).total_seconds() / 3600)
        # Reachability now, not at arrival: a guest who signed up has a verified email
        identity = IDENTITY_POINTS.get(s.identity_level, 0)
        if s.patient_id:
            identity = max(identity, IDENTITY_POINTS["email"])
        parts = {
            "recency": round(40 * 0.5 ** (hours / 24), 1),
            "stage": STAGE_POINTS.get(s.stage, 0),
            "identity": identity,
            "channel": CHANNEL_POINTS.get(s.source_channel, 0),
        }
        consent = _consent(repo, s)
        messages = repo.list_messages(s.id)
        risky = any(m.sender == "patient" and m.risk_level in ("med", "high") for m in messages)
        open_escalation = bool(s.patient_id and repo.open_escalation_for_patient(s.patient_id))

        concern, concern_source = None, None
        if consent["data_sharing"]:
            current = repo.memory_store(s.id).current()
            top = next((i for k in ("chief_complaint", "goal", "symptom") for i in current
                        if i.kind == k and i.status == "active"), None)
            if top:
                concern, concern_source = top.value, "shared by the patient"
        if concern is None and topic_for(s):
            concern, concern_source = topic_for(s), "topic they arrived with"

        contact = repo.get_lead_contact(s.id)
        reachable = bool(s.patient_id or (contact and (contact.volunteered_email or contact.social_handle)))
        lead = {
            "lead_session_id": s.id,
            "channel": s.source_channel,
            "identity_level": s.identity_level,
            "stage": s.stage,
            "last_activity": last,
            "score": round(sum(parts.values()), 1),
            "score_parts": parts,
            "concern": concern,
            "concern_source": concern_source,
            "converted": s.patient_id is not None,
        }
        # Clinical concern: compassion priority. Only visible (and actionable) with consent.
        if consent["data_sharing"] and (risky or open_escalation):
            clinical.append({**lead, "priority": "clinical",
                             "suggestion": "Clinical follow-up through the nurse queue only. No sales contact."})
            continue
        if reachable and (consent["marketing"] or _lead_marketing_consent(repo, s)):
            lead["suggestion"] = "OK to follow up personally (marketing consent on record)."
        elif reachable and consent["data_sharing"]:
            lead["suggestion"] = "Care follow-up only: no marketing consent."
        else:
            lead["suggestion"] = "No contact: no consent to be contacted."
        leads.append(lead)

    leads.sort(key=lambda l: -l["score"])
    return {"leads": leads, "clinical": clinical,
            "scoring": {"recency": "0–40, halves every 24h", "stage": STAGE_POINTS,
                        "identity": IDENTITY_POINTS, "channel": CHANNEL_POINTS}}


def _lead_marketing_consent(repo: Repository, session) -> bool:
    """Marketing consent given before an account exists (e.g. the lead form checkbox)."""
    rows = [c for c in repo.lead_consents(session.id) if c.kind == "marketing"]
    return bool(rows) and sorted(rows, key=lambda c: c.recorded_at)[-1].granted


assert set(STAGE_POINTS) == set(STAGES)
