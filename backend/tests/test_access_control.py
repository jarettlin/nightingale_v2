"""Access control is enforced server-side: every request carries a Supabase JWT,
roles come from our own tables, and ownership is checked on every route."""

import pytest

from app.escalation import Staff
from tests.conftest import bearer, make_token

GUEST_A, GUEST_B = make_token("user-a", anonymous=True), make_token("user-b", anonymous=True)
PATIENT_A = make_token("user-a", email="a@example.com")   # same user id after converting
PATIENT_B = make_token("user-b", email="b@example.com")
NURSE = make_token("nurse-user", email="nurse@clinic.example")
FRONT_DESK = make_token("desk-user", email="desk@clinic.example")
OTHER_CLINIC = make_token("other-user", email="n@other.example")


def start(api, token, text):
    session = api.post("/sessions", json={"source": "instagram", "campaign": "ivf_over40"},
                       headers=bearer(token)).json()["session"]
    api.post(f"/sessions/{session['id']}/messages", json={"text": text}, headers=bearer(token))
    return session["id"]


def become_patient(api, token, session_id, share=True):
    r = api.post(f"/sessions/{session_id}/convert", json={"phone": "+60123456789", "data_sharing": share},
                 headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()["patient_id"]


@pytest.fixture
def world(api, repo):
    repo.add_staff("nurse-user", Staff("staff-nurse", "clinic_demo", "nurse"))
    repo.add_staff("desk-user", Staff("staff-desk", "clinic_demo", "staff"))
    repo.add_staff("other-user", Staff("staff-other", "other_clinic", "nurse"))
    a = start(api, GUEST_A, "I take Advil and I've had a headache for 3 days")
    b = start(api, GUEST_B, "I feel dizzy")
    pa = become_patient(api, PATIENT_A, a)
    pb = become_patient(api, PATIENT_B, b, share=False)
    return {"a": a, "b": b, "pa": pa, "pb": pb}


# ---------------------------------------------------------------- brief requirements

def test_patient_a_cannot_fetch_patient_b_chat_history(api, world):
    assert api.get(f"/sessions/{world['b']}/messages", headers=bearer(PATIENT_A)).status_code == 404
    assert api.get(f"/sessions/{world['b']}/profile", headers=bearer(PATIENT_A)).status_code == 404
    assert api.post(f"/sessions/{world['b']}/messages", json={"text": "hi"},
                    headers=bearer(PATIENT_A)).status_code == 404
    mine = api.get(f"/sessions/{world['a']}/messages", headers=bearer(PATIENT_A))
    assert mine.status_code == 200 and "Advil" in mine.json()["messages"][0]["text"]


def test_patient_cannot_fetch_clinician_triage_queue(api, world):
    for path in ("/staff/escalations", "/staff/patients", f"/staff/patients/{world['pa']}/messages"):
        assert api.get(path, headers=bearer(PATIENT_A)).status_code == 403


def test_staff_nurse_and_clinician_see_all_consented_patients(api, world):
    for token in (NURSE, FRONT_DESK):
        listed = {p["patient_id"] for p in api.get("/staff/patients", headers=bearer(token)).json()["patients"]}
        assert listed == {world["pa"]}  # B declined sharing, so B isn't listed
        thread = api.get(f"/staff/patients/{world['pa']}/messages", headers=bearer(token))
        assert thread.status_code == 200 and thread.json()["messages"][0]["risk_level"] == "low"
        assert api.get(f"/staff/patients/{world['pb']}/messages", headers=bearer(token)).status_code == 404


# ---------------------------------------------------------------- the rest of the boundary

def test_unauthenticated_requests_are_rejected(api, world):
    assert api.post("/sessions", json={}).status_code == 401
    assert api.get(f"/sessions/{world['a']}/messages").status_code == 401
    assert api.get("/staff/escalations").status_code == 401
    assert api.get("/clinic").status_code == 200  # public clinic info only


@pytest.mark.parametrize("token", [
    "not-a-jwt",
    make_token("user-a", minutes=-5),                         # expired
    __import__("jwt").encode({"sub": "user-a", "aud": "authenticated", "exp": 9999999999},
                             "wrong-secret-wrong-secret-wrong-secret", algorithm="HS256"),  # forged
])
def test_bad_tokens_are_rejected(api, world, token):
    assert api.get(f"/sessions/{world['a']}/messages", headers=bearer(token)).status_code == 401


def test_guest_cannot_read_another_guests_session(api):
    a = start(api, GUEST_A, "hello")
    assert api.get(f"/sessions/{a}/messages", headers=bearer(GUEST_B)).status_code == 404


def test_staff_at_another_clinic_see_nothing(api, world):
    assert api.get("/staff/patients", headers=bearer(OTHER_CLINIC)).json()["patients"] == []
    assert api.get(f"/staff/patients/{world['pa']}/messages", headers=bearer(OTHER_CLINIC)).status_code == 404


def test_withdrawn_consent_removes_staff_access(api, repo, world):
    from app.repository import Consent, now
    repo.add_consent(Consent("clinic_demo", "data_sharing", False, "v1", patient_id=world["pa"], recorded_at=now()))
    assert api.get(f"/staff/patients/{world['pa']}/messages", headers=bearer(NURSE)).status_code == 404


def test_role_cannot_be_claimed_in_the_token(api, world):
    import time

    import jwt
    from tests.conftest import TEST_JWT_SECRET
    sneaky = jwt.encode({"sub": "user-a", "aud": "authenticated", "exp": int(time.time()) + 60,
                         "role": "staff", "app_metadata": {"role": "admin"}}, TEST_JWT_SECRET, algorithm="HS256")
    assert api.get("/staff/escalations", headers=bearer(sneaky)).status_code == 403


def test_front_desk_cannot_send_clinical_replies(api, world):
    escalation = api.post(f"/sessions/{world['a']}/escalations", json={}, headers=bearer(PATIENT_A)).json()
    r = api.post(f"/staff/escalations/{escalation['escalation_id']}/responses", json={"body": "hi"},
                 headers=bearer(FRONT_DESK))
    assert r.status_code == 403
