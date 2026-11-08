"""Runs the real pipeline against Supabase. Skipped unless DATABASE_URL is set
(in backend/.env). Everything happens in one transaction that is rolled back,
so the database is left exactly as it was."""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


@pytest.fixture
def db_repo():
    from app.supabase_repo import SupabaseRepo, connect
    conn = connect()
    try:
        yield SupabaseRepo(conn)
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def clinic_id(db_repo):
    row = db_repo.conn.execute("select id from public.clinic where slug = 'sunrise'").fetchone()
    assert row, "seed clinic missing: run supabase/seed.sql"
    return str(row["id"])


def test_pipeline_round_trip_on_supabase(db_repo, clinic_id, llm, cipher):
    from app.pipeline import handle_patient_message

    session = db_repo.create_lead_session(clinic_id=clinic_id, source_channel="instagram_ad_click",
                                          identity_level="anonymous", campaign_id="ivf_over40")
    first = handle_patient_message(db_repo, llm, cipher, session.id, "Hi I'm Sarah Tan, I take Advil")
    second = handle_patient_message(db_repo, llm, cipher, session.id, "Actually I stopped last week")

    stored = db_repo.list_messages(session.id)
    assert [m.sender for m in stored] == ["patient", "assistant", "patient", "assistant"]
    assert cipher.decrypt(stored[0].content_encrypted) == "Hi I'm Sarah Tan, I take Advil"
    assert stored[0].content_redacted == "Hi I'm [REDACTED], I take Advil"
    assert stored[0].risk_level == "low" and stored[0].risk_assessed_at

    [med] = second.profile["medication"]
    assert (med["value"], med["status"], med["versions"]) == ("Advil", "stopped", 2)
    assert first.profile["medication"][0]["provenance_message_id"] == first.patient_message_id

    view = db_repo.conn.execute(
        "select value, status from public.current_memory where lead_session_id = %s", (session.id,)).fetchall()
    assert view == [{"value": "Advil", "status": "stopped"}]

    events = db_repo.conn.execute(
        "select event from public.funnel_event where lead_session_id = %s order by id", (session.id,)).fetchall()
    assert [e["event"] for e in events] == ["visitor", "conversation_started", "value_event", "value_event"]
    assert db_repo.get_lead_session(session.id).stage == "value_event"


def test_conversion_round_trip_on_supabase(db_repo, clinic_id, llm, cipher):
    from app.conversion import ConsentChoices, SignupDetails, convert, start_auth
    from app.pipeline import handle_patient_message
    from app.repository import LeadContact

    session = db_repo.create_lead_session(clinic_id=clinic_id, source_channel="instagram_comment",
                                          identity_level="handle", campaign_id="ivf_over40")
    db_repo.set_lead_contact(LeadContact(session.id, social_platform="instagram", social_handle="@test.handle"))
    guest = handle_patient_message(db_repo, llm, cipher, session.id, "thinking about IVF, irregular periods for 6 months")
    start_auth(db_repo, session.id)

    details = SignupDetails(auth_user_id=None, email="synthetic@example.com",
                            email_verified_at="2026-11-08T10:00:00+00:00", phone="+60123456789")
    result = convert(db_repo, cipher, session.id, details, ConsentChoices(data_sharing=True))
    again = convert(db_repo, cipher, session.id, details, ConsentChoices(data_sharing=True))

    assert again.already_converted and again.patient_id == result.patient_id
    linked = db_repo.get_lead_session(session.id)
    assert linked.patient_id == result.patient_id and linked.converted_at and linked.stage == "patient_created"
    assert result.profile["chief_complaint"][0]["provenance_message_id"] == guest.patient_message_id
    assert {c.kind for c in db_repo.contact_points(result.patient_id)} == {"email", "phone", "instagram"}
    assert [(c.kind, c.granted) for c in db_repo.consents(result.patient_id)] == \
           [("data_sharing", True), ("marketing", False)]
    assert db_repo.find_patient_by_email(clinic_id, "SYNTHETIC@example.com") == result.patient_id

    # RLS sees the consent: the database's own helper agrees the patient consented
    assert db_repo.conn.execute("select app.patient_consented(%s) as ok", (result.patient_id,)).fetchone()["ok"]


def test_escalation_round_trip_on_supabase(db_repo, clinic_id, llm, cipher):
    from app.conversion import ConsentChoices, SignupDetails, convert
    from app.escalation import send_to_clinic
    from app.pipeline import handle_patient_message

    session = db_repo.create_lead_session(clinic_id=clinic_id, source_channel="staff_referral",
                                          identity_level="staff_referred",
                                          referral_topic="asked about egg freezing at today's visit")
    handle_patient_message(db_repo, llm, cipher, session.id, "I take Advil")
    convert(db_repo, cipher, session.id,
            SignupDetails(None, "synthetic2@example.com", "2026-11-08T10:00:00+00:00", "+60123456780"),
            ConsentChoices(data_sharing=True))
    trigger = handle_patient_message(db_repo, llm, cipher, session.id, "my chest feels funny")

    sent = send_to_clinic(db_repo, cipher, session.id)
    again = send_to_clinic(db_repo, cipher, session.id)
    stored = db_repo.get_escalation(sent.escalation.id)

    assert again.already_open and again.escalation.id == stored.id
    assert stored.trigger_message_id == trigger.patient_message_id and stored.reason == "med_risk"
    assert stored.attribution["referral_topic"] == "asked about egg freezing at today's visit"
    assert any("staff referral" in b["text"] for b in stored.triage_summary)
    assert {(i["kind"], i["value"]) for i in stored.profile_snapshot} >= {("medication", "Advil")}
    assert db_repo.get_lead_session(session.id).stage == "escalation_sent"

    db_repo.update_escalation(stored.id, "acknowledged")
    assert db_repo.get_escalation(stored.id).status == "acknowledged"
    assert stored.id in [e.id for e in db_repo.escalation_queue(clinic_id)]   # real escalations may be queued too


def test_citations_are_stored_on_supabase(db_repo, clinic_id, llm, cipher):
    from app.pipeline import handle_patient_message

    session = db_repo.create_lead_session(clinic_id=clinic_id, source_channel="website_widget",
                                          identity_level="anonymous")
    result = handle_patient_message(db_repo, llm, cipher, session.id, "how much does egg freezing cost?")
    rows = db_repo.citations_for(result.assistant_message_id)
    assert rows and rows[0]["source_doc"] == "kb/egg-freezing.md"
    stored = db_repo.get_message(result.assistant_message_id)
    assert stored.reply_confidence == "high" and stored.advice_generated


def test_channels_round_trip_on_supabase(db_repo, clinic_id, cipher):
    from datetime import datetime, timedelta, timezone

    from app.acquisition import claim_link, handle_lead_form
    from app.config import ClinicConfig
    from app.leads import funnel, warm_leads

    clinic = ClinicConfig(clinic_id=clinic_id, name="Sunrise Fertility Clinic", emergency_number="999",
                          response_window="12 to 18 hours")
    now = datetime.now(timezone.utc)
    link = handle_lead_form(db_repo, "synthetic3@example.com", "IUI", True, "form-1", clinic, "http://x")
    token = link.rsplit("/r/", 1)[1]

    # Claim as a real (synthetic) anonymous auth user, so the foreign key holds
    uid = str(db_repo.conn.execute(
        "insert into auth.users (id, aud, role, is_anonymous) values (gen_random_uuid(), 'authenticated', "
        "'authenticated', true) returning id").fetchone()["id"])
    session = claim_link(db_repo, cipher, token, uid, None, clinic, now)
    assert session.guest_auth_user_id == uid and session.identity_level == "email"
    assert db_repo.list_messages(session.id)[0].sender == "assistant"          # stored greeting
    assert [(c.kind, c.granted) for c in db_repo.lead_consents(session.id)] == [("marketing", True)]

    since = now - timedelta(minutes=5)
    f = funnel(db_repo, clinic_id, since)
    assert f["channels"]["lead_form"]["counts"]["visitor"] >= 1
    leads = warm_leads(db_repo, clinic_id, now)["leads"]
    mine = next(l for l in leads if l["lead_session_id"] == session.id)
    assert mine["concern"] == "IUI" and mine["suggestion"].startswith("OK to follow up")


def test_unknown_session_id_is_none(db_repo):
    assert db_repo.get_lead_session("not-a-uuid") is None
    assert db_repo.get_lead_session("00000000-0000-0000-0000-000000000000") is None


# ------------------------------------------------------------------ RLS on the backend's own reads

def _auth_user(db_repo, anonymous=False) -> str:
    import uuid
    uid = str(uuid.uuid4())
    db_repo.conn.execute("insert into auth.users (id, aud, role, is_anonymous) values (%s, 'authenticated', 'authenticated', %s)",
                         (uid, anonymous))
    return uid


def test_backend_reads_are_filtered_by_rls_even_without_python_checks(db_repo, clinic_id, llm, cipher):
    """read_as() is the second lock: these calls skip every ownership check in the API
    on purpose, and the database still only returns what that person may see."""
    from app.conversion import ConsentChoices, SignupDetails, convert
    from app.memory import profile
    from app.pipeline import handle_patient_message

    owner, intruder, nurse = _auth_user(db_repo, anonymous=True), _auth_user(db_repo, anonymous=True), _auth_user(db_repo)
    db_repo.conn.execute("insert into public.staff_member (auth_user_id, clinic_id, role, display_name) "
                         "values (%s, %s, 'nurse', 'Test Nurse')", (nurse, clinic_id))
    session = db_repo.create_lead_session(clinic_id=clinic_id, source_channel="direct",
                                          identity_level="anonymous", guest_auth_user_id=owner)
    handle_patient_message(db_repo, llm, cipher, session.id, "I take Advil")

    def seen_by(uid):
        with db_repo.read_as(uid):
            return (len(db_repo.list_messages(session.id)),
                    len(profile(db_repo.memory_store(session.id)).get("medication", [])))

    assert seen_by(owner) == (2, 1)
    assert seen_by(intruder) == (0, 0)          # another guest: nothing, even with the exact session id
    assert seen_by(nurse) == (0, 0)             # staff: not before the patient converts and consents

    convert(db_repo, cipher, session.id, SignupDetails(owner, "rls@example.com", "2026-11-08T10:00:00+00:00", "+60123456781"),
            ConsentChoices(data_sharing=False))
    everything = seen_by(owner)
    assert everything[0] >= 2 and everything[1] == 1   # same auth user after conversion: still theirs
    assert seen_by(nurse) == (0, 0)             # converted but declined sharing

    patient_id = db_repo.get_lead_session(session.id).patient_id
    from app.conversion import update_consent
    update_consent(db_repo, patient_id, clinic_id, session.id, data_sharing=True, marketing=False)
    assert seen_by(nurse) == seen_by(owner) == everything   # consent given: now the clinic sees the same


def test_read_as_switches_back_and_survives_errors(db_repo):
    import psycopg
    who = lambda: db_repo.conn.execute("select current_user as u").fetchone()["u"]
    owner_role = who()
    uid = _auth_user(db_repo, anonymous=True)

    with db_repo.read_as(uid):
        assert who() == "authenticated"
    assert who() == owner_role                  # the owner role is back for the writes that follow

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db_repo.read_as(uid):
            db_repo.conn.execute("select * from public.audit_log")   # no grant for users at all
    assert who() == owner_role                  # the failed read didn't poison the request's transaction
    db_repo.add_audit("system", "still_works")
