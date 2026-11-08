import pytest

from app.conversion import ConsentChoices, SignupDetails, convert
from app.escalation import (EscalationError, NeedsAccount, NeedsConsent, Staff, move, respond, review_packet,
                            send_to_clinic)
from app.pipeline import handle_patient_message


def make_patient(repo, cipher, session, share=True):
    details = SignupDetails(auth_user_id="auth-1", email="p@example.com",
                            email_verified_at="2026-11-08T10:00:00+00:00", phone="+60123456789")
    return convert(repo, cipher, session.id, details, ConsentChoices(data_sharing=share))


@pytest.fixture
def patient_session(repo, llm, cipher, guest_session):
    handle_patient_message(repo, llm, cipher, guest_session.id, "I take Advil. I've had a headache for 3 days")
    make_patient(repo, cipher, guest_session)
    return guest_session


NURSE = Staff(id="nurse-1", clinic_id="clinic_demo", role="nurse")


def test_payload_persists_everything_the_brief_requires(repo, llm, cipher, patient_session):
    turn = handle_patient_message(repo, llm, cipher, patient_session.id, "my chest feels funny since this morning")
    result = send_to_clinic(repo, cipher, patient_session.id)
    stored = repo.get_escalation(result.escalation.id)

    # Triggering message
    assert stored.trigger_message_id == turn.patient_message_id
    assert (stored.reason, stored.risk_level, stored.status) == ("med_risk", "med", "sent")

    # Triage summary: 1–5 bullets, each traceable to real messages
    assert 1 <= len(stored.triage_summary) <= 5
    assert stored.triage_summary[0]["text"].startswith("Why now: ambiguous chest symptom")
    message_ids = {m.id for m in repo.messages}
    for bullet in stored.triage_summary:
        assert set(bullet["provenance_message_ids"]) <= message_ids

    # Profile snapshot with provenance, including facts from before signup
    snapshot = {(i["kind"], i["value"]): i for i in stored.profile_snapshot}
    advil = snapshot[("medication", "Advil")]
    assert repo.get_message(advil["provenance_message_id"]).sent_as == "guest"
    assert snapshot[("chief_complaint", "headache")]["detail"] == {"onset": "3 days"}

    # Acquisition context
    assert stored.attribution["source_channel"] == "instagram_ad_click"
    assert stored.attribution["campaign_id"] == "ivf_over40"
    assert stored.attribution["lead_session_id"] == patient_session.id

    # Room for the clinician stage
    assert stored.response_due_at > stored.created_at and repo.clinician_responses(stored.id) == []


def test_snapshot_is_frozen_at_send_time(repo, llm, cipher, patient_session):
    result = send_to_clinic(repo, cipher, patient_session.id)
    handle_patient_message(repo, llm, cipher, patient_session.id, "Actually I stopped last week")
    snap = {i["value"]: i["status"] for i in repo.get_escalation(result.escalation.id).profile_snapshot}
    assert snap["Advil"] == "active"  # what the patient had said when they pressed Send


def test_confirmation_is_honest_about_timing(repo, llm, cipher, patient_session):
    handle_patient_message(repo, llm, cipher, patient_session.id, "I feel dizzy")
    result = send_to_clinic(repo, cipher, patient_session.id)
    assert "12 to 18 hours" in result.confirmation and "keep chatting" in result.confirmation
    assert repo.messages[-1].sender == "system"


def test_high_risk_points_to_emergency_care_not_a_wait(repo, llm, cipher, patient_session):
    handle_patient_message(repo, llm, cipher, patient_session.id, "I have crushing chest pain")
    result = send_to_clinic(repo, cipher, patient_session.id)
    assert result.escalation.reason == "high_risk"
    assert "999" in result.confirmation and "don't wait" in result.confirmation
    assert "12 to 18" not in result.confirmation


def test_patient_can_keep_chatting_and_does_not_create_duplicates(repo, llm, cipher, patient_session):
    first = send_to_clinic(repo, cipher, patient_session.id)
    later = handle_patient_message(repo, llm, cipher, patient_session.id, "what are your opening hours?")
    assert later.reply  # chat continues after sending
    again = send_to_clinic(repo, cipher, patient_session.id)
    assert again.already_open and again.escalation.id == first.escalation.id
    assert len(repo.escalations) == 1


def test_guest_must_continue_securely_first(repo, llm, cipher, guest_session):
    handle_patient_message(repo, llm, cipher, guest_session.id, "I feel dizzy")
    with pytest.raises(NeedsAccount):
        send_to_clinic(repo, cipher, guest_session.id)


def test_no_send_without_data_sharing_consent(repo, llm, cipher, guest_session):
    handle_patient_message(repo, llm, cipher, guest_session.id, "I feel dizzy")
    make_patient(repo, cipher, guest_session, share=False)
    with pytest.raises(NeedsConsent):
        send_to_clinic(repo, cipher, guest_session.id)


def test_cannot_escalate_someone_elses_message(repo, llm, cipher, patient_session):
    other = repo.create_lead_session(clinic_id="clinic_demo", source_channel="direct", identity_level="anonymous")
    foreign = handle_patient_message(repo, llm, cipher, other.id, "I feel dizzy")
    with pytest.raises(EscalationError, match="someone else"):
        send_to_clinic(repo, cipher, patient_session.id, trigger_message_id=foreign.patient_message_id)


def test_diagnosis_request_is_its_own_reason(repo, llm, cipher, patient_session):
    handle_patient_message(repo, llm, cipher, patient_session.id, "do I have endometriosis?")
    result = send_to_clinic(repo, cipher, patient_session.id)
    assert result.escalation.reason == "wants_diagnosis"
    assert "wants diagnosis" in result.escalation.triage_summary[-1]["text"]


def test_clinician_review_without_the_patient_repeating_anything(repo, llm, cipher, patient_session):
    result = send_to_clinic(repo, cipher, patient_session.id)
    packet = review_packet(repo, cipher, NURSE, result.escalation.id)
    assert "I take Advil. I've had a headache for 3 days" in [s["text"] for s in packet["sources"].values()]

    respond(repo, cipher, NURSE, result.escalation.id, "Thanks for the detail. Can you come in Tuesday?")
    stored = repo.get_escalation(result.escalation.id)
    assert (stored.status, stored.assigned_to) == ("responded", "nurse-1")
    reply = repo.messages[-1]
    assert reply.sender == "clinician" and cipher.decrypt(reply.content_encrypted).startswith("Thanks")
    move(repo, NURSE, stored.id, "closed")
    assert repo.escalation_queue("clinic_demo") == []


def test_clinician_stage_rules(repo, llm, cipher, patient_session):
    result = send_to_clinic(repo, cipher, patient_session.id)
    front_desk = Staff(id="staff-1", clinic_id="clinic_demo", role="staff")
    other_clinic = Staff(id="nurse-9", clinic_id="other", role="nurse")

    move(repo, front_desk, result.escalation.id, "acknowledged")           # front desk may acknowledge
    with pytest.raises(EscalationError, match="only nurses"):
        respond(repo, cipher, front_desk, result.escalation.id, "hi")       # but not give clinical replies
    with pytest.raises(EscalationError, match="not found"):
        review_packet(repo, cipher, other_clinic, result.escalation.id)     # other clinics can't see it
    with pytest.raises(EscalationError, match="cannot move"):
        move(repo, NURSE, result.escalation.id, "responded")                # must review before responding


def test_queue_puts_high_risk_first(repo, llm, cipher, patient_session):
    send_to_clinic(repo, cipher, patient_session.id)  # med/low first
    other = repo.create_lead_session(clinic_id="clinic_demo", source_channel="direct", identity_level="anonymous",
                                     guest_auth_user_id="auth-2")
    handle_patient_message(repo, llm, cipher, other.id, "I want to hurt myself")
    convert(repo, cipher, other.id, SignupDetails("auth-2", "q@example.com", "2026-11-08T10:00:00+00:00", "+60111"),
            ConsentChoices(data_sharing=True))
    send_to_clinic(repo, cipher, other.id)
    assert [e.risk_level for e in repo.escalation_queue("clinic_demo")][0] == "high"


def test_audit_and_events_hold_no_content(repo, llm, cipher, patient_session):
    import json
    send_to_clinic(repo, cipher, patient_session.id)
    dumped = json.dumps(repo.audit_log) + json.dumps(repo.funnel_events)
    assert "Advil" not in dumped and "headache" not in dumped
    assert "escalation_sent" in [e["event"] for e in repo.funnel_events]
