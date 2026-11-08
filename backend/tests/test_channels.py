"""Channels: rules config, staff referral links, social comment DMs, lead forms,
funnel per channel and warm leads."""

import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest

from app.channels import load_rules, opening
from app.config import DEFAULT_CLINIC
from app.escalation import Staff
from app.repository import LeadSession
from tests.conftest import TEST_META_SECRET, bearer, make_token

NOON_KL = datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)      # Tuesday 12:00 in Kuala Lumpur
NIGHT_KL = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)    # Tuesday 23:00
SAT_EVENING_KL = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)

NURSE = make_token("nurse-user", email="nurse@clinic.example")
GUEST = make_token("guest-1", anonymous=True)


@pytest.fixture
def staffed(api, repo):
    repo.add_staff("nurse-user", Staff("staff-nurse", "clinic_demo", "nurse"))
    return api


def signed(payload: dict) -> tuple[bytes, dict]:
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(TEST_META_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"x-hub-signature-256": sig, "content-type": "application/json"}


def ig_comment(handle="mei.tries", text="does egg freezing hurt?", comment_id="c1"):
    return {"object": "instagram", "entry": [{"id": "page-1", "time": 1, "changes": [{
        "field": "comments",
        "value": {"id": comment_id, "text": text, "from": {"id": "u1", "username": handle}, "media": {"id": "post-42"}},
    }]}]}


# ------------------------------------------------------------------ rules config

def test_same_question_from_two_channels_opens_differently():
    referral = LeadSession("1", "c", "staff_referral", "staff_referred", referral_topic="egg freezing")
    ad = LeadSession("2", "c", "instagram_ad_click", "anonymous", campaign_id="ivf_over40")
    a, b = opening(referral, DEFAULT_CLINIC, NOON_KL), opening(ad, DEFAULT_CLINIC, NOON_KL)
    assert a.rule_id != b.rule_id and a.greeting != b.greeting
    assert "egg freezing" in a.greeting and "IVF after 40" in b.greeting


@pytest.mark.parametrize("note", ["asked about egg freezing at today's visit",
                                  "Patient asked about egg freezing at today's visit",
                                  "egg freezing at today's visit"])
def test_staff_note_wording_never_doubles_up_in_the_greeting(note):
    # Found by hand: "you asked about asked about egg freezing"
    o = opening(LeadSession("1", "c", "staff_referral", "staff_referred", referral_topic=note), DEFAULT_CLINIC, NOON_KL)
    assert "asked about egg freezing at today's visit" in o.greeting and "asked about asked" not in o.greeting.lower()


def test_identified_lead_is_never_asked_for_what_they_gave():
    o = opening(LeadSession("1", "c", "lead_form", "email", referral_topic="IUI"), DEFAULT_CLINIC, NOON_KL)
    assert "email" in o.skip and "fill anything in again" in o.greeting


@pytest.mark.parametrize("when, expect_note, back", [
    (NOON_KL, False, None), (NIGHT_KL, True, "tomorrow at 08:30"), (SAT_EVENING_KL, True, "Monday at 08:30")])
def test_time_of_day_is_part_of_the_rules(when, expect_note, back):
    o = opening(LeadSession("1", "c", "website_widget", "anonymous"), DEFAULT_CLINIC, when)
    assert ("offline" in o.greeting) is expect_note
    if back:
        assert back in o.greeting


def test_rules_live_in_one_file_with_a_catch_all():
    rules = load_rules()["rules"]
    assert rules[-1]["when"] == {} and len({r["id"] for r in rules}) == len(rules)


def test_new_sessions_store_the_channel_greeting(api):
    body = api.post("/sessions", json={"source": "instagram", "campaign": "ivf_over40"}, headers=bearer(GUEST)).json()
    assert "IVF after 40" in body["greeting"]["text"]
    thread = api.get(f"/sessions/{body['session']['id']}/messages", headers=bearer(GUEST)).json()["messages"]
    assert thread[0]["id"] == body["greeting"]["id"]   # resumes with the same greeting on any device


# ------------------------------------------------------------------ staff referral (mandatory contract)

def test_staff_referral_link_opens_already_knowing_the_topic(staffed, repo):
    made = staffed.post("/staff/referrals", json={"topic": "asked about egg freezing at today's visit"},
                        headers=bearer(NURSE)).json()
    token = made["link"].rsplit("/r/", 1)[1]
    session = repo.find_session_by_token_hash(hashlib.sha256(token.encode()).hexdigest())
    assert session.source_channel == "staff_referral" and session.referred_by == "staff-nurse"
    assert [e for e in repo.funnel_events if e["lead_session_id"] == session.id] == []  # no visit until opened

    claimed = staffed.post("/sessions/claim", json={"token": token}, headers=bearer(GUEST)).json()
    assert claimed["skip"] == ["topic"]
    thread = staffed.get(f"/sessions/{claimed['session']['id']}/messages", headers=bearer(GUEST)).json()["messages"]
    assert "asked about egg freezing at today's visit" in thread[0]["text"]
    assert [e["event"] for e in repo.funnel_events if e["lead_session_id"] == session.id] == ["visitor"]


def test_names_in_staff_notes_are_redacted(staffed):
    made = staffed.post("/staff/referrals", json={"topic": "Mrs Siti Aminah asked about IVF"}, headers=bearer(NURSE)).json()
    assert "Siti" not in made["topic"] and "IVF" in made["topic"]


def test_a_referral_link_belongs_to_whoever_opens_it_first(staffed):
    token = staffed.post("/staff/referrals", json={"topic": "IUI"}, headers=bearer(NURSE)).json()["link"].rsplit("/r/", 1)[1]
    assert staffed.post("/sessions/claim", json={"token": token}, headers=bearer(GUEST)).status_code == 200
    assert staffed.post("/sessions/claim", json={"token": token}, headers=bearer(GUEST)).status_code == 200  # same person
    other = make_token("someone-else", anonymous=True)
    assert staffed.post("/sessions/claim", json={"token": token}, headers=bearer(other)).status_code == 404
    assert staffed.post("/sessions/claim", json={"token": "not-a-real-token-at-all"}, headers=bearer(other)).status_code == 404


def test_only_staff_can_make_referral_links(api):
    assert api.post("/staff/referrals", json={"topic": "IVF"}, headers=bearer(GUEST)).status_code == 403


# ------------------------------------------------------------------ social comment (mandatory contract)

def test_comment_webhook_sends_one_private_reply_with_a_link(api, repo):
    raw, headers = signed(ig_comment())
    assert api.post("/webhooks/meta", content=raw, headers=headers).json() == {"processed": 1, "results": ["replied"]}
    [dm] = api.app.state.deps.sender.outbox
    assert dm["comment_id"] == "c1" and "/r/" in dm["text"] and "egg freezing" in dm["text"]

    [session] = [s for s in repo.sessions.values() if s.source_channel == "instagram_comment"]
    assert session.identity_level == "handle" and session.campaign_id == "post-42"
    assert repo.get_lead_contact(session.id).social_handle == "mei.tries"
    # The comment itself is public and personal: we keep the topic, not the words
    stored = json.dumps([m.__dict__ for m in repo.messages]) + json.dumps(repo.audit_log, default=str)
    assert "does egg freezing hurt" not in stored


def test_the_same_person_commenting_again_is_not_messaged_again(api):
    for cid in ("c1", "c2"):
        raw, headers = signed(ig_comment(comment_id=cid))
        last = api.post("/webhooks/meta", content=raw, headers=headers).json()
    assert last["results"] == ["skipped_recent"] and len(api.app.state.deps.sender.outbox) == 1


def test_unsigned_or_forged_webhooks_are_rejected(api):
    raw = json.dumps(ig_comment()).encode()
    assert api.post("/webhooks/meta", content=raw).status_code == 401
    assert api.post("/webhooks/meta", content=raw, headers={"x-hub-signature-256": "sha256=deadbeef"}).status_code == 401


def test_meta_subscription_handshake(api):
    ok = api.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert api.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                             "hub.challenge": "42"}).status_code == 403


def test_opening_a_comment_link_continues_privately(api):
    raw, headers = signed(ig_comment(text="how much is IVF?"))
    api.post("/webhooks/meta", content=raw, headers=headers)
    token = api.app.state.deps.sender.outbox[0]["text"].split("/r/")[1].split()[0]
    claimed = api.post("/sessions/claim", json={"token": token}, headers=bearer(GUEST)).json()
    first = api.get(f"/sessions/{claimed['session']['id']}/messages", headers=bearer(GUEST)).json()["messages"][0]
    assert "Nothing you write here is public" in first["text"] and "IVF" in first["text"]


# ------------------------------------------------------------------ lead form

def test_lead_form_prefills_email_and_records_marketing_consent(api, repo):
    created = api.post("/webhooks/lead-form", json={"email": "aisha@example.com", "topic": "IUI", "marketing_consent": True},
                       headers={"x-webhook-secret": "lead-form-secret"})
    token = created.json()["link"].rsplit("/r/", 1)[1]
    claimed = api.post("/sessions/claim", json={"token": token}, headers=bearer(GUEST)).json()
    assert claimed["skip"] == ["email"]
    sid = claimed["session"]["id"]
    assert repo.get_lead_contact(sid).volunteered_email == "aisha@example.com"
    assert [(c.kind, c.granted) for c in repo.lead_consents(sid)] == [("marketing", True)]


@pytest.mark.parametrize("headers, body, status", [
    ({}, {"email": "a@example.com"}, 401),
    ({"x-webhook-secret": "wrong"}, {"email": "a@example.com"}, 401),
    ({"x-webhook-secret": "lead-form-secret"}, {"email": "not-an-email"}, 422),
])
def test_lead_form_needs_the_secret_and_a_real_email(api, headers, body, status):
    assert api.post("/webhooks/lead-form", json=body, headers=headers).status_code == status


# ------------------------------------------------------------------ funnel and warm leads

def test_funnel_counts_unique_sessions_per_channel(staffed):
    for _ in range(2):
        g = make_token(f"g-{_}", anonymous=True)
        sid = staffed.post("/sessions", json={"source": "instagram"}, headers=bearer(g)).json()["session"]["id"]
        staffed.post(f"/sessions/{sid}/messages", json={"text": "what time do you open on saturday"}, headers=bearer(g))
    staffed.post("/sessions", json={"source": "google"}, headers=bearer(make_token("g-x", anonymous=True)))

    f = staffed.get("/staff/funnel", headers=bearer(NURSE)).json()
    ig = f["channels"]["instagram_ad_click"]
    assert ig["counts"]["visitor"] == 2 and ig["counts"]["value_event"] == 2 and ig["rates"]["value_event"] == 1.0
    assert f["channels"]["google_ad_click"]["counts"]["conversation_started"] == 0


def test_warm_leads_are_scored_transparently_and_respect_consent(staffed, repo):
    referral = staffed.post("/staff/referrals", json={"topic": "egg freezing"}, headers=bearer(NURSE)).json()
    staffed.post("/sessions/claim", json={"token": referral["link"].rsplit("/r/", 1)[1]}, headers=bearer(GUEST))
    anon = make_token("anon-2", anonymous=True)
    sid = staffed.post("/sessions", json={}, headers=bearer(anon)).json()["session"]["id"]
    staffed.post(f"/sessions/{sid}/messages", json={"text": "I'm 38 and have irregular periods"}, headers=bearer(anon))

    body = staffed.get("/staff/leads", headers=bearer(NURSE)).json()
    first = body["leads"][0]
    assert first["channel"] == "staff_referral"                     # warmer channel, met in person
    assert first["score"] == round(sum(first["score_parts"].values()), 1)
    assert first["concern"] == "egg freezing" and first["concern_source"] == "topic they arrived with"
    walk_in = next(l for l in body["leads"] if l["lead_session_id"] == sid)
    assert walk_in["concern"] is None                               # what they typed stays private before consent
    assert walk_in["suggestion"].startswith("No contact")


def test_clinical_concern_is_compassion_priority_not_sales(staffed):
    g = make_token("g-clin", anonymous=True)
    sid = staffed.post("/sessions", json={}, headers=bearer(g)).json()["session"]["id"]
    staffed.post(f"/sessions/{sid}/messages", json={"text": "my chest feels funny"}, headers=bearer(g))
    p = make_token("g-clin", email="clin@example.com")
    staffed.post(f"/sessions/{sid}/convert", json={"phone": "+60123456789", "data_sharing": True, "marketing": True},
                 headers=bearer(p))

    body = staffed.get("/staff/leads", headers=bearer(NURSE)).json()
    assert sid not in [l["lead_session_id"] for l in body["leads"]]   # never in the sales list, even with marketing consent
    [clinical] = [l for l in body["clinical"] if l["lead_session_id"] == sid]
    assert clinical["priority"] == "clinical" and "No sales contact" in clinical["suggestion"]
    assert clinical["score_parts"]["identity"] == 15   # reachable by verified email now, though they arrived anonymous


def test_leads_and_funnel_are_staff_only(api):
    assert api.get("/staff/leads", headers=bearer(GUEST)).status_code == 403
    assert api.get("/staff/funnel", headers=bearer(GUEST)).status_code == 403
