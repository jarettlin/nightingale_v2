"""The demo seed runs through the real pipeline, so it doubles as an end-to-end check."""

from app.crypto import ephemeral_cipher
from app.demo import seed
from app.repository import InMemoryRepo


def test_demo_seed_produces_a_realistic_queue():
    repo = InMemoryRepo()
    seed(repo, ephemeral_cipher())
    queue = repo.escalation_queue("clinic_demo")
    assert [e.reason for e in queue] == ["high_risk", "med_risk", "wants_diagnosis"]
    staff_referral = next(e for e in queue if e.attribution["source_channel"] == "staff_referral")
    assert staff_referral.attribution["referral_topic"] == "asked about egg freezing at today's visit"
    assert any("staff referral" in b["text"] for b in staff_referral.triage_summary)
    # Facts said before sign-up keep pointing at those guest messages
    for e in queue:
        for item in e.profile_snapshot:
            assert repo.get_message(item["provenance_message_id"]) is not None
