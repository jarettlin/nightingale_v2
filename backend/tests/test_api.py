"""The full journey over HTTP: guest -> value -> offer -> sign-in -> consent -> send -> nurse reply."""

from app.escalation import Staff
from tests.conftest import bearer, make_token

GUEST = make_token("user-1", anonymous=True)
PATIENT = make_token("user-1", email="mei@example.com")
NURSE = make_token("nurse-user", email="nurse@clinic.example")


def test_full_journey(api, repo):
    repo.add_staff("nurse-user", Staff("staff-nurse", "clinic_demo", "nurse"))

    created = api.post("/sessions", json={"source": "instagram", "campaign": "ivf_over40", "creative": "reel_03"},
                       headers=bearer(GUEST))
    assert created.status_code == 201
    sid = created.json()["session"]["id"]
    assert created.json()["session"]["source_channel"] == "instagram_ad_click"
    assert "999" in created.json()["emergency_notice"]

    # Value first, no sign-up asked on landing
    first = api.post(f"/sessions/{sid}/messages", json={"text": "how much does egg freezing cost?"},
                     headers=bearer(GUEST)).json()
    assert first["risk_level"] == "low" and first["conversion_offer"].startswith("Want to keep")

    concern = api.post(f"/sessions/{sid}/messages", json={"text": "I've had irregular periods for 6 months"},
                       headers=bearer(GUEST)).json()
    assert concern["profile"]["chief_complaint"][0]["value"] == "irregular periods"

    # Guest pressing Send is asked to continue securely, not refused
    blocked = api.post(f"/sessions/{sid}/escalations", json={}, headers=bearer(GUEST))
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "needs_account"

    # Converting needs a verified (non-anonymous) session
    assert api.post(f"/sessions/{sid}/convert", json={"phone": "+60123456789", "data_sharing": True},
                    headers=bearer(GUEST)).status_code == 401
    assert api.post(f"/sessions/{sid}/auth-started", headers=bearer(GUEST)).status_code == 204
    converted = api.post(f"/sessions/{sid}/convert", json={"phone": "+60123456789", "data_sharing": True},
                         headers=bearer(PATIENT)).json()
    assert "irregular periods" in converted["opening_message"]

    worry = api.post(f"/sessions/{sid}/messages", json={"text": "my chest feels funny"}, headers=bearer(PATIENT)).json()
    assert worry["show_send_to_clinic"] and worry["conversion_offer"] is None

    sent = api.post(f"/sessions/{sid}/escalations", json={}, headers=bearer(PATIENT))
    assert sent.status_code == 201 and "12 to 18 hours" in sent.json()["confirmation"]
    eid = sent.json()["escalation_id"]

    # Nurse picks it up from the queue and replies
    q = api.get("/staff/escalations", headers=bearer(NURSE)).json()["escalations"]
    assert [e["id"] for e in q] == [eid] and q[0]["summary"][0].startswith("Why now")
    packet = api.get(f"/staff/escalations/{eid}", headers=bearer(NURSE)).json()
    assert "I've had irregular periods for 6 months" in [s["text"] for s in packet["sources"].values()]
    assert api.post(f"/staff/escalations/{eid}/responses", json={"body": "Can you come in on Tuesday?"},
                    headers=bearer(NURSE)).status_code == 201

    mine = api.get("/me/escalations", headers=bearer(PATIENT)).json()["escalations"]
    assert mine[0]["status"] == "responded" and mine[0]["responses"][0]["text"] == "Can you come in on Tuesday?"
    thread = api.get(f"/sessions/{sid}/messages", headers=bearer(PATIENT)).json()["messages"]
    assert thread[-1]["sender"] == "clinician"


def test_patient_on_a_new_device_finds_their_conversation(api):
    guest = make_token("user-9", anonymous=True)
    sid = api.post("/sessions", json={"source": "instagram"}, headers=bearer(guest)).json()["session"]["id"]
    api.post(f"/sessions/{sid}/messages", json={"text": "how much does egg freezing cost?"}, headers=bearer(guest))
    patient = make_token("user-9", email="nine@example.com")
    api.post(f"/sessions/{sid}/convert", json={"phone": "+60123456789", "data_sharing": True}, headers=bearer(patient))

    assert api.get("/me/sessions", headers=bearer(patient)).json()["sessions"][0]["id"] == sid
    assert api.get("/me/sessions", headers=bearer(make_token("someone", anonymous=True))).json()["sessions"] == []


def test_email_link_in_another_browser_finds_the_same_conversation(api):
    # Found by hand: the confirmation link opened in a browser with no saved session id,
    # and the app started an empty conversation instead of resuming
    guest = make_token("user-12", anonymous=True)
    sid = api.post("/sessions", json={"source": "instagram"}, headers=bearer(guest)).json()["session"]["id"]
    api.post(f"/sessions/{sid}/messages", json={"text": "how much does egg freezing cost?"}, headers=bearer(guest))

    verified = make_token("user-12", email="twelve@example.com")   # same user, email now confirmed
    [found] = api.get("/me/sessions", headers=bearer(verified)).json()["sessions"]
    assert found == {**found, "id": sid, "converted": False}
    assert api.get(f"/sessions/{sid}/messages", headers=bearer(verified)).status_code == 200


def test_signed_in_non_patient_owns_the_sessions_they_start(api):
    # Found by hand: such sessions were created with no owner at all
    verified = make_token("user-13", email="thirteen@example.com")
    sid = api.post("/sessions", json={}, headers=bearer(verified)).json()["session"]["id"]
    assert api.post(f"/sessions/{sid}/messages", json={"text": "hi"}, headers=bearer(verified)).status_code == 200
    assert api.get("/me/sessions", headers=bearer(verified)).json()["sessions"][0]["id"] == sid


def test_guest_messages_are_rate_limited(api):
    sid = api.post("/sessions", json={}, headers=bearer(GUEST)).json()["session"]["id"]
    codes = [api.post(f"/sessions/{sid}/messages", json={"text": "hi"}, headers=bearer(GUEST)).status_code
             for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429
    other = make_token("someone-else", anonymous=True)
    sid2 = api.post("/sessions", json={}, headers=bearer(other)).json()["session"]["id"]
    assert api.post(f"/sessions/{sid2}/messages", json={"text": "hi"}, headers=bearer(other)).status_code == 200


def test_auth_outage_is_reported_honestly(api):
    from app.auth import AuthUnavailable

    def down(_token):
        raise AuthUnavailable("jwks unreachable")
    api.app.state.deps.verifier.verify = down
    r = api.post("/sessions", json={}, headers=bearer(GUEST))
    assert r.status_code == 503 and "999" in r.json()["detail"]


def test_message_length_is_bounded(api):
    sid = api.post("/sessions", json={}, headers=bearer(GUEST)).json()["session"]["id"]
    assert api.post(f"/sessions/{sid}/messages", json={"text": "x" * 2001}, headers=bearer(GUEST)).status_code == 422
    assert api.post(f"/sessions/{sid}/messages", json={"text": ""}, headers=bearer(GUEST)).status_code == 422


def test_staff_cannot_start_patient_sessions(api, repo):
    repo.add_staff("nurse-user", Staff("staff-nurse", "clinic_demo", "nurse"))
    assert api.post("/sessions", json={}, headers=bearer(NURSE)).status_code == 403
