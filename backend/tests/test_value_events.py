"""Value events: every statistic traces to a live query; generated messages are
tracked and checked for accuracy and privacy."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from app.value import STAT_MIN, people_this_week
from tests.conftest import bearer, make_token


def chat(api, n_start=0, count=1, text="what time do you open on saturday"):
    ids = []
    for i in range(n_start, n_start + count):
        g = make_token(f"visitor-{i}", anonymous=True)
        sid = api.post("/sessions", json={}, headers=bearer(g)).json()["session"]["id"]
        api.post(f"/sessions/{sid}/messages", json={"text": text}, headers=bearer(g))
        ids.append((g, sid))
    return ids


# ------------------------------------------------------------------ live statistic

def test_statistic_is_a_live_count_and_is_audited(api, repo):
    chat(api, count=6)
    body = api.post("/sessions", json={}, headers=bearer(make_token("newcomer", anonymous=True))).json()

    recount = people_this_week(repo, "clinic_demo", datetime.now(timezone.utc))
    assert body["stat"] == {"text": f"{recount} people asked Sunrise Fertility Clinic a question this week.",
                            "kind": "count", "value": recount}
    assert recount == 6
    # Every displayed number is recorded with what was counted, so it can be re-checked later
    shown = [a for a in repo.audit_log if a["action"] == "live_stat_shown"]
    assert shown[-1]["metadata"]["value"] == recount


def test_small_numbers_are_never_shown(api):
    chat(api, count=STAT_MIN - 1)
    stat = api.post("/sessions", json={}, headers=bearer(make_token("newcomer", anonymous=True))).json()["stat"]
    assert stat["kind"] == "alternative" and stat["value"] is None
    assert not re.search(r"\d+ (people|person)", stat["text"])          # a truthful alternative, not a number
    assert "12 to 18 hours" in stat["text"]


def test_count_only_includes_this_week_and_this_clinic(api, repo):
    sessions = chat(api, count=STAT_MIN)
    last_month = {sid for _, sid in sessions[:2]}                        # two of them chatted last month
    for e in repo.funnel_events:
        if e["lead_session_id"] in last_month:
            e["created_at"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    other = repo.create_lead_session(clinic_id="other_clinic", source_channel="direct", identity_level="anonymous",
                                     guest_auth_user_id="elsewhere")
    repo.add_funnel_event(other.id, "conversation_started")
    assert people_this_week(repo, "clinic_demo", datetime.now(timezone.utc)) == STAT_MIN - 2


def test_one_person_with_several_chats_counts_once(api):
    # Found in a real run: the first count said "10 people" when two testers had made 10 chats
    same = make_token("same-person", anonymous=True)
    for _ in range(STAT_MIN + 2):
        sid = api.post("/sessions", json={}, headers=bearer(same)).json()["session"]["id"]
        api.post(f"/sessions/{sid}/messages", json={"text": "hi"}, headers=bearer(same))
    stat = api.post("/sessions", json={}, headers=bearer(make_token("newcomer", anonymous=True))).json()["stat"]
    assert stat["kind"] == "alternative"                                 # 1 person, not 7


def test_a_statistic_view_is_not_counted_as_help(api, repo):
    chat(api, count=STAT_MIN)
    api.post("/sessions", json={}, headers=bearer(make_token("newcomer", anonymous=True)))
    assert not any(e["value_kind"] == "live_stat" for e in repo.funnel_events)


# ------------------------------------------------------------------ shareable note

def test_share_note_is_short_cited_unbranded_and_tracked(api, repo):
    [(g, sid)] = chat(api, text="My name is John Doe, IC S1234567A. I'm interested in egg freezing.")
    r = api.post(f"/sessions/{sid}/share-note", headers=bearer(g)).json()

    assert r["chars"] == len(r["text"]) <= 240
    assert "egg freezing" in r["text"] and "Well done" in r["text"]
    fact = r["text"].split(". ", 1)[1].split(" A question worth asking")[0]
    assert fact in r["source"]["excerpt"]                                 # the fact is the clinic's own words
    for private in ("John", "Doe", "S1234567A", "Sunrise"):               # nothing personal, no branding
        assert private not in r["text"]
    events = [(e["event"], e["value_kind"]) for e in repo.funnel_events if e["lead_session_id"] == sid]
    assert ("value_event", "articulation_draft") in events


def test_no_note_without_a_topic(api):
    [(g, sid)] = chat(api, text="hi")
    assert api.post(f"/sessions/{sid}/share-note", headers=bearer(g)).status_code == 409


def test_cannot_make_notes_from_someone_elses_session(api):
    [(_, sid)] = chat(api, text="I'm interested in IVF")
    assert api.post(f"/sessions/{sid}/share-note", headers=bearer(make_token("intruder", anonymous=True))).status_code == 404


# ------------------------------------------------------------------ earned email

def test_earned_email_is_derived_from_the_session_and_leaves_personal_details_out(api, repo):
    [(g, sid)] = chat(api, text="I'm interested in egg freezing. I take metformin and get bad headaches. My name is John Doe")
    r = api.post(f"/sessions/{sid}/email-summary", json={"email": "mei@example.com"}, headers=bearer(g))
    assert r.status_code == 200

    [mail] = api.app.state.deps.email_sender.outbox
    assert mail["to"] == "mei@example.com" and "egg freezing" in mail["subject"]
    assert len(re.findall(r"^\s+\d\. ", mail["body"], re.M)) == 6        # six questions people forget to ask
    for private in ("metformin", "headache", "John", "Doe"):
        assert private.lower() not in mail["body"].lower()
    assert "won't email you again" in mail["body"]                         # transactional only

    assert repo.get_lead_contact(sid).volunteered_email == "mei@example.com"   # prefilled at sign-up later
    assert repo.lead_consents(sid) == []                                    # an email request is not marketing consent
    events = [(e["event"], e["value_kind"]) for e in repo.funnel_events if e["lead_session_id"] == sid]
    assert ("email_captured", None) in events and ("value_event", "conversation_summary") in events


def test_marketing_needs_its_own_checkbox_and_is_timestamped(api, repo):
    [(g, sid)] = chat(api, text="I'm interested in IVF")
    api.post(f"/sessions/{sid}/email-summary", json={"email": "a@example.com", "marketing_consent": True}, headers=bearer(g))
    [consent] = repo.lead_consents(sid)
    assert (consent.kind, consent.granted) == ("marketing", True) and consent.recorded_at and consent.policy_version
    assert "stop them any time" in api.app.state.deps.email_sender.outbox[0]["body"]


def test_summary_emails_are_validated_and_rate_limited(api):
    [(g, sid)] = chat(api, text="I'm interested in IUI")
    assert api.post(f"/sessions/{sid}/email-summary", json={"email": "nope"}, headers=bearer(g)).status_code == 422
    codes = [api.post(f"/sessions/{sid}/email-summary", json={"email": "a@example.com"}, headers=bearer(g)).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]   # 3 attempts an hour, invalid ones included: it's an abuse limit


@pytest.mark.parametrize("topic_text, key", [
    ("thinking about IVF", "IVF"), ("my periods are irregular", "irregular periods"),
    ("we've been trying to conceive", "trying to conceive"), ("what is an AMH test", "fertility testing"),
])
def test_every_topic_produces_a_note_that_fits(api, topic_text, key):
    [(g, sid)] = chat(api, text=topic_text)
    r = api.post(f"/sessions/{sid}/share-note", headers=bearer(g))
    assert r.status_code == 200 and r.json()["topic"] == key and r.json()["chars"] <= 240
