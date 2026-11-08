import pytest

from tests.conftest import bearer, make_token

GUEST = make_token("guest-1", anonymous=True)


def ask(api, text):
    session = api.post("/sessions", json={}, headers=bearer(GUEST)).json()["session"]
    return api.post(f"/sessions/{session['id']}/messages", json={"text": text}, headers=bearer(GUEST)).json()


@pytest.mark.parametrize("question", [
    "Are you a real doctor?",
    "am i talking to a human?",
    "is this a bot",
    "who am I talking to?",
])
def test_honest_answer_about_what_the_ai_is(api, model, question):
    body = ask(api, question)
    reply = body["reply"]["text"]

    # What the AI is
    assert reply.startswith("No, I'm not a doctor") and "AI assistant" in reply
    assert "can't diagnose" in reply
    # What the clinic is
    assert "Sunrise Fertility Clinic" in reply and "fertility clinic" in reply
    # When a human gets involved
    assert "Send to Clinic" in reply and "nurse" in reply and "12 to 18 hours" in reply
    assert "999" in reply
    assert body["show_send_to_clinic"] is True

    # Fixed wording, not generated: the answer model was never asked
    assert all(p["system"].startswith("You assess clinical risk") for p in model.prompts)


def test_urgent_message_wins_over_identity_question(api):
    body = ask(api, "are you a real doctor? I have crushing chest pain")
    assert body["risk_level"] == "high" and "999" in body["reply"]["text"]
    assert "AI assistant" not in body["reply"]["text"]
