import json
import logging

import pytest

from app.pipeline import SessionExpired, SessionNotFound, handle_patient_message


def send(repo, llm, cipher, session, text):
    return handle_patient_message(repo, llm, cipher, session.id, text)


def test_original_is_encrypted_and_redacted_copy_is_stored(repo, llm, cipher, guest_session):
    result = send(repo, llm, cipher, guest_session, "Hi I'm Sarah Tan, I take Advil")
    stored = next(m for m in repo.messages if m.id == result.patient_message_id)

    assert "Sarah" not in stored.content_encrypted
    assert cipher.decrypt(stored.content_encrypted) == "Hi I'm Sarah Tan, I take Advil"
    assert stored.content_redacted == "Hi I'm [REDACTED], I take Advil"
    assert stored.redaction_entities == [{"kind": "NAME", "start": 7, "end": 16}]


def test_risk_fields_are_persisted_on_the_patient_message(repo, llm, cipher, guest_session):
    result = send(repo, llm, cipher, guest_session, "I have crushing chest pain")
    patient = next(m for m in repo.messages if m.id == result.patient_message_id)
    reply = next(m for m in repo.messages if m.id == result.assistant_message_id)

    assert (patient.risk_level, patient.escalation_required) == ("high", True)
    assert patient.risk_reason and patient.risk_confidence and patient.risk_assessed_at
    assert patient.risk_rule_codes == ["cardiac"]
    assert reply.in_reply_to == patient.id
    assert reply.advice_generated is False and reply.reply_confidence == "high"
    assert result.show_send_to_clinic


def test_memory_provenance_points_at_the_stored_guest_message(repo, llm, cipher, guest_session):
    result = send(repo, llm, cipher, guest_session, "I take Advil")
    [med] = result.profile["medication"]
    source = next(m for m in repo.messages if m.id == med["provenance_message_id"])
    assert source.id == result.patient_message_id
    assert source.sent_as == "guest" and source.sender == "patient"


def test_high_risk_messages_still_feed_memory(repo, llm, cipher, guest_session):
    result = send(repo, llm, cipher, guest_session, "severe abdominal pain after egg retrieval, I take Advil")
    assert result.risk.risk_level.label == "high"
    assert result.profile["medication"][0]["value"] == "Advil"


def test_funnel_events_and_stage(repo, llm, cipher, guest_session):
    send(repo, llm, cipher, guest_session, "what are your opening hours?")
    send(repo, llm, cipher, guest_session, "and how much is egg freezing?")
    send(repo, llm, cipher, guest_session, "I feel dizzy")  # med risk: no value event

    events = [(e["event"], e["value_kind"]) for e in repo.funnel_events]
    assert events == [("visitor", None), ("conversation_started", None),
                      ("value_event", "service_answer"), ("value_event", "service_answer")]
    assert all(e["source_channel"] == "instagram_ad_click" for e in repo.funnel_events)
    assert repo.get_lead_session(guest_session.id).stage == "value_event"


def test_stage_never_moves_backwards(repo):
    s = repo.create_lead_session(clinic_id="c", source_channel="direct", identity_level="anonymous")
    repo.advance_stage(s.id, "consented")
    repo.advance_stage(s.id, "conversation_started")
    assert repo.get_lead_session(s.id).stage == "consented"


def test_ambiguous_correction_adds_a_clarifying_question(repo, llm, cipher, guest_session):
    send(repo, llm, cipher, guest_session, "I take Advil and metformin")
    result = send(repo, llm, cipher, guest_session, "I stopped last week")
    assert result.clarify and result.reply.endswith(result.clarify)


def test_unknown_and_purged_sessions_are_refused(repo, llm, cipher, guest_session):
    with pytest.raises(SessionNotFound):
        handle_patient_message(repo, llm, cipher, "nope", "hello")
    guest_session.purged_at = "2026-11-01T00:00:00+00:00"
    with pytest.raises(SessionExpired):
        send(repo, llm, cipher, guest_session, "hello")


def test_memory_follows_the_patient_across_sessions(repo, llm, cipher, guest_session):
    send(repo, llm, cipher, guest_session, "I take Advil")
    guest_session.patient_id = "patient-1"   # conversion (full flow comes later)
    later = repo.create_lead_session(clinic_id="clinic_demo", source_channel="staff_referral",
                                     identity_level="staff_referred", referral_topic="egg freezing",
                                     patient_id="patient-1")
    result = send(repo, llm, cipher, later, "Actually I stopped last week")

    [med] = result.profile["medication"]
    assert med["status"] == "stopped" and med["versions"] == 2
    first = next(m for m in repo.messages if m.sent_as == "guest")
    assert first.lead_session_id == guest_session.id  # never copied, still the guest's message


def test_no_raw_text_in_audit_events_or_logs(repo, llm, cipher, guest_session, caplog):
    caplog.set_level(logging.INFO, logger="nightingale")
    send(repo, llm, cipher, guest_session, "My name is John Doe, IC S1234567A, I take Advil")
    dumped = json.dumps(repo.audit_log) + json.dumps(repo.funnel_events) + caplog.text
    for secret in ("John", "S1234567A", "Advil"):
        assert secret not in dumped


def test_model_never_sees_the_name(repo, model, llm, cipher, guest_session):
    send(repo, llm, cipher, guest_session, "hi im jarett and i take Advil")
    assert model.prompts and all("jarett" not in p["user"] for p in model.prompts)
