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


def test_unknown_session_id_is_none(db_repo):
    assert db_repo.get_lead_session("not-a-uuid") is None
    assert db_repo.get_lead_session("00000000-0000-0000-0000-000000000000") is None
