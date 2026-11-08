import pytest

from app.conversion import (ConsentChoices, ConversionError, SignupDetails, conversion_offer, convert,
                            fields_to_collect, start_auth)
from app.memory import history
from app.pipeline import handle_patient_message
from app.repository import ContactPoint, LeadContact, now

VERIFIED = "2026-11-08T10:00:00+00:00"


def signup(auth="auth-user-1", email="sarah@example.com", phone="+60123456789", verified=VERIFIED):
    return SignupDetails(auth_user_id=auth, email=email, email_verified_at=verified, phone=phone)


@pytest.fixture
def instagram_guest(repo):
    # ?source=instagram&campaign=ivf_over40
    return repo.create_lead_session(clinic_id="clinic_demo", source_channel="instagram_ad_click",
                                    identity_level="anonymous", campaign_id="ivf_over40",
                                    creative="reel_03", guest_auth_user_id="auth-user-1")


def test_brief_scenario_instagram_guest_converts_with_context_intact(repo, llm, cipher, instagram_guest):
    concern = "I'm 41 and thinking about IVF. I've had irregular periods for 6 months."
    guest_turn = handle_patient_message(repo, llm, cipher, instagram_guest.id, concern)
    guest_message_id = guest_turn.patient_message_id

    start_auth(repo, instagram_guest.id)
    result = convert(repo, cipher, instagram_guest.id, signup(), ConsentChoices(data_sharing=True))

    # Context is in the PatientSession
    session = repo.get_lead_session(instagram_guest.id)
    assert session.patient_id == result.patient_id and session.converted_at
    assert result.profile["chief_complaint"][0]["value"] == "irregular periods"
    assert result.profile["goal"][0]["value"] == "IVF"

    # Provenance resolves to the original GuestMessage
    source = next(m for m in repo.messages if m.id == result.profile["chief_complaint"][0]["provenance_message_id"])
    assert source.id == guest_message_id and source.sent_as == "guest"
    assert cipher.decrypt(source.content_encrypted) == concern

    # Attribution retained
    attribution = session.attribution()
    assert (attribution["source_channel"], attribution["campaign_id"], attribution["creative"]) == \
           ("instagram_ad_click", "ivf_over40", "reel_03")

    # Concern never re-asked: the first message shows what we know and asks nothing
    assert "irregular periods" in result.opening_message and "IVF" in result.opening_message
    assert "?" not in result.opening_message
    assert "repeat" in result.opening_message

    # The next message is a patient message and builds on the guest's facts
    after = handle_patient_message(repo, llm, cipher, instagram_guest.id, "I take folic acid daily")
    assert next(m for m in repo.messages if m.id == after.patient_message_id).sent_as == "patient"
    assert {"irregular periods", "Folic Acid"} <= {i["value"] for items in after.profile.values() for i in items}

    events = [e["event"] for e in repo.funnel_events]
    i = events.index("auth_started")
    assert events[i:i + 3] == ["auth_started", "consented", "patient_created"]
    assert events.index("value_event") < i  # value came before we asked for anything


def test_unverified_email_creates_nothing(repo, cipher, instagram_guest):
    with pytest.raises(ConversionError, match="verified"):
        convert(repo, cipher, instagram_guest.id, signup(verified=None), ConsentChoices(data_sharing=True))
    assert repo.patients == {} and repo.get_lead_session(instagram_guest.id).patient_id is None


def test_retrying_an_interrupted_signup_is_safe(repo, cipher, instagram_guest):
    first = convert(repo, cipher, instagram_guest.id, signup(), ConsentChoices(data_sharing=True))
    again = convert(repo, cipher, instagram_guest.id, signup(), ConsentChoices(data_sharing=True))
    assert again.patient_id == first.patient_id and again.already_converted
    assert len(repo.patients) == 1 and len(repo.consent_rows) == 2  # data_sharing + marketing, once


def test_returning_patient_on_a_new_device_links_to_the_same_record(repo, llm, cipher, instagram_guest):
    handle_patient_message(repo, llm, cipher, instagram_guest.id, "I take Advil")
    first = convert(repo, cipher, instagram_guest.id, signup(), ConsentChoices(data_sharing=True))

    later = repo.create_lead_session(clinic_id="clinic_demo", source_channel="website_widget",
                                     identity_level="anonymous", guest_auth_user_id="anon-on-laptop")
    second = convert(repo, cipher, later.id, signup(auth=None), ConsentChoices(data_sharing=True))

    assert second.patient_id == first.patient_id and not second.created
    assert second.profile["medication"][0]["value"] == "Advil"  # memory follows the patient


def test_patient_can_choose_not_to_carry_a_fact_over(repo, llm, cipher, instagram_guest):
    turn = handle_patient_message(repo, llm, cipher, instagram_guest.id, "I take Advil and I've had a headache for 2 days")
    advil_id = turn.profile["medication"][0]["id"]

    result = convert(repo, cipher, instagram_guest.id, signup(),
                     ConsentChoices(data_sharing=True, exclude_memory_ids=[advil_id]))

    assert "medication" not in result.profile and "headache" in result.opening_message
    store = repo.memory_store(instagram_guest.id)
    retracted = [i for i in store.all() if i.supersedes_id == advil_id][0]
    assert retracted.status == "retracted" and retracted.extracted_by == "patient_edit"
    note = next(m for m in repo.messages if m.id == retracted.provenance_message_id)
    assert note.sender == "system"
    assert [v.status for v in history(store, retracted.id)] == ["active", "retracted"]


def test_marketing_consent_is_separate_and_timestamped(repo, cipher, instagram_guest):
    result = convert(repo, cipher, instagram_guest.id, signup(), ConsentChoices(data_sharing=True))
    rows = {c.kind: c for c in repo.consents(result.patient_id)}
    assert rows["data_sharing"].granted is True
    assert rows["marketing"].granted is False           # never implied by data sharing
    assert rows["marketing"].recorded_at and rows["marketing"].policy_version


def test_declining_data_sharing_is_respected(repo, cipher, instagram_guest):
    result = convert(repo, cipher, instagram_guest.id, signup(), ConsentChoices(data_sharing=False))
    assert "consented" not in [e["event"] for e in repo.funnel_events]
    assert "haven't shared" in result.opening_message
    assert repo.consents(result.patient_id)[0].granted is False


def test_identified_lead_is_not_asked_for_their_email_again(repo):
    lead = repo.create_lead_session(clinic_id="clinic_demo", source_channel="lead_form", identity_level="email")
    repo.set_lead_contact(LeadContact(lead.id, volunteered_email="mei@example.com"))
    assert fields_to_collect(repo, lead) == {"ask": ["phone"], "prefill": {"email": "mei@example.com"}}


def test_social_handle_becomes_a_contact_point(repo, cipher):
    lead = repo.create_lead_session(clinic_id="clinic_demo", source_channel="instagram_comment", identity_level="handle")
    repo.set_lead_contact(LeadContact(lead.id, social_platform="instagram", social_handle="@mei.tries"))
    result = convert(repo, cipher, lead.id, signup(), ConsentChoices(data_sharing=True))
    kinds = {c.kind: c.value for c in repo.contact_points(result.patient_id)}
    assert kinds == {"email": "sarah@example.com", "phone": "+60123456789", "instagram": "@mei.tries"}


def test_offer_appears_only_after_value_or_clinical_intent(repo, cipher, instagram_guest):
    assert conversion_offer(instagram_guest, value_delivered=False, escalation_offered=False) is None
    assert "Continue securely" in conversion_offer(instagram_guest, value_delivered=True, escalation_offered=False)
    assert "nurse" in conversion_offer(instagram_guest, value_delivered=False, escalation_offered=True)
    convert(repo, cipher, instagram_guest.id, signup(), ConsentChoices(data_sharing=True))
    assert conversion_offer(repo.get_lead_session(instagram_guest.id),
                            value_delivered=True, escalation_offered=True) is None


def test_expired_session_cannot_convert(repo, cipher, instagram_guest):
    instagram_guest.purged_at = now()
    with pytest.raises(ConversionError, match="expired"):
        convert(repo, cipher, instagram_guest.id, signup(), ConsentChoices(data_sharing=True))


def test_changing_a_phone_number_keeps_history(repo, cipher, instagram_guest):
    result = convert(repo, cipher, instagram_guest.id, signup(), ConsentChoices(data_sharing=True))
    repo.upsert_contact_point(ContactPoint(result.patient_id, "phone", "+6598765432"))
    phones = [c for c in repo.contact_points(result.patient_id) if c.kind == "phone"]
    assert [(p.value, p.valid_to is None) for p in phones] == [("+60123456789", False), ("+6598765432", True)]
