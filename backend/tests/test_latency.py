"""Latency: stalled model calls are cut short, and fact extraction runs alongside
the risk check and reply instead of after them."""

import json
import threading

from app.extraction import EXTRACT_SYSTEM
from app.grounding import VERIFY_SYSTEM
from app.intake import RISK_SYSTEM
from app.llm import TIMEOUTS, SafeLLM
from app.pipeline import handle_patient_message


class RecordingClient:
    def __init__(self):
        self.timeouts = {}

    def complete(self, system, user, timeout_s=10.0, json_mode=False):
        purpose = ("risk" if system == RISK_SYSTEM else "extract" if system == EXTRACT_SYSTEM
                   else "verify" if system == VERIFY_SYSTEM else "reply")
        self.timeouts[purpose] = timeout_s
        if purpose == "risk":
            return json.dumps({"risk_level": "low", "reason": "t", "confidence": "high"})
        if purpose == "extract":
            return json.dumps({"operations": []})
        if purpose == "verify":
            return json.dumps({"results": [{"n": 1, "supported": True}]})
        return "The clinic is open Monday to Friday from 8:30am to 5:30pm. [S1]"


def test_each_call_type_gets_its_own_short_timeout(repo, cipher, guest_session):
    client = RecordingClient()
    llm = SafeLLM(client)
    handle_patient_message(repo, llm, cipher, guest_session.id, "what are your opening hours?", extractor_llm=llm)
    assert client.timeouts == {p: TIMEOUTS[p] for p in ("risk", "reply", "extract", "verify")}
    assert all(t < 15 for t in client.timeouts.values())  # the old timeout that let a stall last 16s


class GatedClient(RecordingClient):
    """The risk call won't answer until the extraction call has started. If the
    pipeline ran them one after the other, the gate would time out."""

    def __init__(self):
        super().__init__()
        self.extract_started = threading.Event()
        self.overlapped = False

    def complete(self, system, user, timeout_s=10.0, json_mode=False):
        if system == EXTRACT_SYSTEM:
            self.extract_started.set()
        elif system == RISK_SYSTEM:
            self.overlapped = self.extract_started.wait(timeout=2)
        return super().complete(system, user, timeout_s, json_mode)


def test_extraction_runs_alongside_the_risk_check(repo, cipher, guest_session):
    client = GatedClient()
    llm = SafeLLM(client)
    result = handle_patient_message(repo, llm, cipher, guest_session.id, "I take Advil, what are your hours?",
                                    extractor_llm=llm)
    assert client.overlapped
    assert result.reply and result.patient_message_id


def test_facts_still_point_at_the_stored_message_when_extracted_in_parallel(repo, llm, cipher, guest_session):
    result = handle_patient_message(repo, llm, cipher, guest_session.id, "I take Advil")
    assert result.profile["medication"][0]["provenance_message_id"] == result.patient_message_id


class StalledExtractor(RecordingClient):
    """Extraction hangs (as seen live: 7s) until the test lets it go."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def complete(self, system, user, timeout_s=10.0, json_mode=False):
        if system == EXTRACT_SYSTEM:
            self.release.wait(timeout=10)
            return json.dumps({"operations": [{"op": "add", "kind": "medication", "value": "LATE-RESULT"}]})
        return super().complete(system, user, timeout_s, json_mode)


def test_a_stalled_extraction_never_holds_up_a_finished_reply(repo, cipher, guest_session):
    import time
    client = StalledExtractor()
    llm = SafeLLM(client)
    started = time.perf_counter()
    try:
        result = handle_patient_message(repo, llm, cipher, guest_session.id, "I take metformin, what are your hours?",
                                        extractor_llm=llm, extraction_grace_s=0.2)
        elapsed = time.perf_counter() - started
    finally:
        client.release.set()

    assert elapsed < 2 and result.reply and result.assistant_message_id   # the reply didn't wait for the stall
    [med] = result.profile["medication"]                             # the offline rules still saved the fact,
    assert med["value"].lower() == "metformin"                       # pointing at the right message
    assert med["provenance_message_id"] == result.patient_message_id


def test_the_late_model_result_is_never_applied(repo, cipher, guest_session):
    import time
    client = StalledExtractor()
    llm = SafeLLM(client)
    result = handle_patient_message(repo, llm, cipher, guest_session.id, "I take metformin",
                                    extractor_llm=llm, extraction_grace_s=0.1)
    client.release.set()
    time.sleep(0.2)                                                  # let the stalled call finish in the background
    values = [m["value"] for m in result.profile.get("medication", [])]
    stored = [i.value for i in repo.memory_store(guest_session.id).all()]
    assert "LATE-RESULT" not in values + stored
