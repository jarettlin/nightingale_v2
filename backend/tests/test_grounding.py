"""Grounding: answers come only from the clinic's own documents, every citation
resolves to a real span of a real file, and anything unsupported is dropped."""

import hashlib
from pathlib import Path

import pytest

from app.grounding import check_reply
from app.knowledge import default_kb
from app.llm import SafeLLM
from app.pipeline import handle_patient_message
from tests.conftest import RoutingLLM, bearer, make_token

BACKEND = Path(__file__).parent.parent
KB = default_kb()


def sources(q):
    return KB.search(q)


# ------------------------------------------------------------- real spans

def test_every_chunk_is_an_exact_span_of_its_file():
    for c in KB.chunks:
        raw = (BACKEND / c.doc).read_text(encoding="utf-8")
        assert raw[c.start:c.end] == c.text
        assert c.version == hashlib.sha256(raw.encode()).hexdigest()[:12]


def test_stored_citations_resolve_to_the_text_the_patient_was_shown(repo, llm, cipher, guest_session):
    result = handle_patient_message(repo, llm, cipher, guest_session.id, "how much does egg freezing cost?")
    stored = repo.citations_for(result.assistant_message_id)
    assert stored and result.reply_confidence == "high"
    for row in stored:
        raw = (BACKEND / row["source_doc"]).read_text(encoding="utf-8")
        span = raw[row["start_char"]:row["end_char"]]
        assert span and row["source_version"] == hashlib.sha256(raw.encode()).hexdigest()[:12]
    cited = result.citations[0]
    assert "RM 12,000" in cited["excerpt"] and "RM 12,000" in result.reply


# ------------------------------------------------------------- retrieval

@pytest.mark.parametrize("question, expected_heading", [
    ("what services do you offer?", "Services we offer"),
    ("do you do sperm freezing?", "Services we do not offer"),
    ("where are you located", "Location"),
    ("what time do you open on saturday", "Opening hours"),
    ("how much does IVF cost", "Cost"),
    ("what is an AMH test", "The AMH test"),
])
def test_retrieval_finds_the_right_section(question, expected_heading):
    assert expected_heading in [c.heading for c in sources(question)]


@pytest.mark.parametrize("question", ["hi", "what is the capital of france", "thanks!"])
def test_retrieval_returns_nothing_when_nothing_fits(question):
    assert sources(question) == []


# ------------------------------------------------------------- checking replies

def test_fully_supported_reply_is_kept_with_high_confidence():
    src = sources("how does egg freezing work and how long does it take?")
    by_heading = {c.heading: n for n, c in enumerate(src, 1)}
    reply = (f"Treatment usually starts with about 10 to 14 days of daily hormone injections. "
             f"[S{by_heading['How the process works']}] From the first injection to egg collection usually "
             f"takes around two weeks. [S{by_heading['How long it takes']}]")
    g = check_reply(reply, src)
    assert g.grounded and g.dropped == 0 and g.confidence == "high" and len(g.citations) == 2


def test_invented_services_are_dropped():
    # The real reply OpenAI gave before grounding existed
    reply = ("We offer fertility evaluations, ovulation tracking, hormone testing, IVF, IUI, egg and sperm "
             "freezing, and fertility counseling. [S1]")
    g = check_reply(reply, sources("what services do you offer?"))
    assert not g.grounded and g.text == ""


def test_invented_numbers_are_dropped_even_when_cited():
    src = sources("how much does egg freezing cost?")
    g = check_reply("One egg freezing cycle costs about RM 9,000. [S1]", src)
    assert not g.grounded
    g = check_reply("The process takes about 2 to 3 weeks from the first injection. [S1]",
                    sources("how long does egg freezing take"))
    assert not g.grounded


@pytest.mark.parametrize("claim, expect_kept", [
    ("We offer sperm freezing and surrogacy.", False),           # source says we do NOT
    ("We do not offer sperm freezing or surrogacy.", True),
    ("We don't offer egg freezing.", False),                    # source says we DO
    ("A GP referral is not required to book a first consultation.", True),
    # The real reply OpenAI gave: one sentence joining a "we don't" and a "we can"
    ("We do not offer sperm freezing at Sunrise Fertility Clinic, but we can refer you to partner "
     "centres that provide this service.", True),
    ("We offer sperm freezing, but we can refer you to partner centres.", False),
])
def test_claims_must_agree_with_the_source_on_not(claim, expect_kept):
    src = sources("what services do you offer and do I need a referral")
    # cite every source so only meaning, not the marker, decides
    markers = "".join(f" [S{n}]" for n in range(1, len(src) + 1))
    assert check_reply(claim + markers, src).grounded is expect_kept


def test_grouped_markers_count_as_separate_citations():
    # The form OpenAI actually used: [S2,S3]
    src = sources("I'm 38, I've had irregular periods for 6 months")
    n = {c.heading: i for i, c in enumerate(src, 1)}
    reply = (f"It is worth seeing a doctor if your periods have become irregular, or if you are trying to "
             f"conceive and your cycles are irregular. [S{n['When to talk to a doctor']},S{n['What counts as irregular']}]")
    g = check_reply(reply, src)
    assert g.grounded and {m for m, _ in g.citations} == {f"S{n['When to talk to a doctor']}", f"S{n['What counts as irregular']}"}
    assert "," not in g.text.split("irregular.")[-1]  # re-emitted as [S#] [S#] for the frontend


def test_removing_a_marker_leaves_no_stray_comma():
    src = sources("what time do you open on saturday")
    g = check_reply("The clinic is open on Saturday from 9am to 1pm, [S1].", src)
    assert g.grounded and ",." not in g.text and g.text.startswith("The clinic is open on Saturday from 9am to 1pm.")


def test_numbers_from_the_patient_may_be_echoed_but_not_invented():
    src = sources("I'm 38, I've had irregular periods for 6 months")
    n = {c.heading: i for i, c in enumerate(src, 1)}[ "When to talk to a doctor"]
    claim = f"At 38, it is worth seeing a doctor since your periods have become irregular. [S{n}]"
    assert check_reply(claim, src, patient_text="I'm 38 and my periods are irregular").grounded
    assert not check_reply(claim, src, patient_text="my periods are irregular").grounded


def test_citations_to_sources_never_sent_are_removed():
    g = check_reply("We offer egg freezing and IVF. [S9]", sources("what services do you offer?"))
    assert not g.grounded and "[S9]" not in g.text


def test_unsupported_sentence_is_dropped_but_supported_ones_stay():
    src = sources("what services do you offer?")
    offer = next(n for n, c in enumerate(src, 1) if c.heading == "Services we offer")
    reply = f"We offer egg freezing, IVF and IUI. [S{offer}] We also offer acupuncture and massage. Happy to help!"
    g = check_reply(reply, src)
    assert g.grounded and g.dropped == 1 and g.confidence == "med"
    assert "acupuncture" not in g.text and "Happy to help!" in g.text


def test_marker_after_full_stop_belongs_to_previous_sentence():
    src = sources("what services do you offer?")
    n = {c.heading: i for i, c in enumerate(src, 1)}
    reply = (f"We offer fertility assessments, egg freezing, IVF and IUI. [S{n['Services we offer']}] "
             f"We do not offer sperm freezing or donor eggs. [S{n['Services we do not offer']}]")
    assert check_reply(reply, src).dropped == 0


# ------------------------------------------------------------- end to end

def test_ungrounded_model_reply_becomes_an_honest_answer(repo, cipher, guest_session):
    liar = SafeLLM(RoutingLLM(answer="We offer acupuncture, sperm freezing and surrogacy. [S1]"))
    result = handle_patient_message(repo, liar, cipher, guest_session.id, "what services do you offer?")
    assert "rather not guess" in result.reply and not result.advice_generated
    assert result.reply_confidence == "low" and result.citations == []
    assert "value_event" not in [e["event"] for e in repo.funnel_events]


def test_off_topic_question_never_reaches_the_reply_model(repo, model, llm, cipher, guest_session):
    result = handle_patient_message(repo, llm, cipher, guest_session.id, "what is the capital of france?")
    assert "rather not guess" in result.reply
    assert not any("SOURCES:" in p["system"] for p in model.prompts)


def test_follow_up_question_uses_conversation_context(repo, model, llm, cipher, guest_session):
    handle_patient_message(repo, llm, cipher, guest_session.id, "how does egg freezing work?")
    result = handle_patient_message(repo, llm, cipher, guest_session.id, "how much does it cost?")
    assert result.citations[0]["source_doc"] == "kb/egg-freezing.md"   # "it" found from the previous turn
    reply_prompt = next(p for p in reversed(model.prompts) if "SOURCES:" in p["system"])
    assert "CONVERSATION SO FAR" in reply_prompt["user"] and "egg freezing" in reply_prompt["user"]
    assert "[S1]" not in reply_prompt["user"].split("LATEST MESSAGE")[0]  # old markers stripped


def test_a_complete_short_question_is_not_glued_to_the_previous_topic(repo, llm, cipher, guest_session):
    # Found live: this question was searched together with the egg-freezing message
    # before it, came back with egg-freezing documents, and no answer could be cited.
    handle_patient_message(repo, llm, cipher, guest_session.id,
                           "I take metformin and my periods are irregular, does that affect egg freezing?")
    result = handle_patient_message(repo, llm, cipher, guest_session.id, "how much does IVF cost?")
    assert result.citations and result.citations[0]["source_doc"] == "kb/ivf.md"
    assert "18,000" in result.reply


def test_api_returns_citations_for_the_frontend(api):
    guest = make_token("g-1", anonymous=True)
    sid = api.post("/sessions", json={}, headers=bearer(guest)).json()["session"]["id"]
    body = api.post(f"/sessions/{sid}/messages", json={"text": "what time do you open on saturday"},
                    headers=bearer(guest)).json()
    [citation] = body["reply"]["citations"]
    assert citation["heading"] == "Opening hours" and citation["source_doc"] == "kb/hours-and-booking.md"
    assert body["reply"]["confidence"] == "high" and "[S1]" in body["reply"]["text"]


# ------------------------------------------------------------- model claim check

import json  # noqa: E402

from app.grounding import VERIFY_SYSTEM, verify_claims  # noqa: E402
from app.intake import handle_turn  # noqa: E402
from app.llm import TIMEOUTS, FakeLLM  # noqa: E402


class Checker:
    """Answers only the claim check, with scripted verdicts (n -> supported)."""

    def __init__(self, verdicts=None, raw=None):
        self.verdicts, self.raw, self.prompts = verdicts or {}, raw, []

    def complete(self, system, user, timeout_s=10.0, json_mode=False):
        assert system == VERIFY_SYSTEM
        self.prompts.append({"user": user, "json_mode": json_mode, "timeout_s": timeout_s})
        if self.raw is not None:
            return self.raw
        return json.dumps({"results": [{"n": n, "supported": ok} for n, ok in self.verdicts.items()]})


def _process_reply():
    src = sources("how does egg freezing work and how long does it take?")
    n = {c.heading: i for i, c in enumerate(src, 1)}
    # Sentence 2 shares nearly all its words with the source, so the rules keep it,
    # but "painless" and "most patients" are the model's own additions.
    reply = (f"Treatment usually starts with about 10 to 14 days of daily hormone injections. [S{n['How the process works']}] "
             f"Most patients find the daily hormone injections painless. [S{n['How the process works']}] "
             f"Happy to help!")
    return src, reply


def test_a_reworded_outside_claim_passes_the_rules_but_not_the_checker():
    src, reply = _process_reply()
    by_rules = check_reply(reply, src)
    assert "painless" in by_rules.text and by_rules.dropped == 0      # the gap the checker closes

    checker = Checker({1: True, 2: False})
    g = verify_claims(by_rules, src, SafeLLM(checker))
    assert "painless" not in g.text and "10 to 14 days" in g.text and "Happy to help!" in g.text
    assert g.grounded and g.verified and g.dropped == 1 and g.confidence == "med"
    # Only factual sentences are checked, in strict JSON mode with its own short timeout
    [call] = checker.prompts
    assert call["json_mode"] and call["timeout_s"] == TIMEOUTS["verify"]
    assert "1. Treatment usually" in call["user"] and "Happy to help" not in call["user"].split("STATEMENTS:")[1]


def test_when_every_claim_is_rejected_the_patient_gets_an_honest_answer():
    src, reply = _process_reply()
    llm = SafeLLM(_Router(reply, verdicts={1: False, 2: False}))
    turn = handle_turn("how does egg freezing work?", llm, sources=src)
    assert "rather not guess" in turn.reply and not turn.advice_generated and turn.reply_confidence == "low"


def test_a_statement_the_checker_skips_counts_as_unsupported():
    src, reply = _process_reply()
    g = verify_claims(check_reply(reply, src), src, SafeLLM(Checker({1: True})))   # said nothing about 2
    assert "painless" not in g.text and g.dropped == 1


def test_the_checker_can_only_remove_never_add():
    src, reply = _process_reply()
    sneaky = Checker(raw=json.dumps({"results": [{"n": 1, "supported": True}, {"n": 2, "supported": True},
                                                 {"n": 3, "supported": True, "text": "We guarantee success."}]}))
    g = verify_claims(check_reply(reply, src), src, SafeLLM(sneaky))
    assert "guarantee" not in g.text and g.text == check_reply(reply, src).text


@pytest.mark.parametrize("client", [FakeLLM(fail=True), Checker(raw="not json"), Checker(raw='{"results": "x"}')])
def test_checker_outage_keeps_the_rule_checked_reply_but_never_high_confidence(client):
    src, reply = _process_reply()
    g = verify_claims(check_reply(reply, src), src, SafeLLM(client))
    assert g.text == check_reply(reply, src).text and g.verified is False and g.confidence == "med"


def test_the_checker_never_sees_identifiers():
    src = sources("how does egg freezing work?")
    checker = Checker({1: True})
    g = check_reply("Treatment usually starts with about 10 to 14 days of daily hormone injections. [S1]"
                    if src[0].heading == "How the process works" else f"{src[0].text.split('. ')[0]}. [S1]", src)
    verify_claims(g, src, SafeLLM(checker), patient_text="My name is Sarah Tan, how does egg freezing work?",
                  known_identifiers=["sarah.tan@example.com"])
    assert "Sarah" not in checker.prompts[0]["user"] and "[REDACTED]" in checker.prompts[0]["user"]


class _Router:
    """Risk: low. Reply: the scripted reply. Claim check: scripted verdicts."""

    def __init__(self, reply, verdicts):
        self.reply, self.checker = reply, Checker(verdicts)

    def complete(self, system, user, timeout_s=10.0, json_mode=False):
        from app.intake import RISK_SYSTEM
        if system == RISK_SYSTEM:
            return json.dumps({"risk_level": "low", "reason": "t", "confidence": "high"})
        if system == VERIFY_SYSTEM:
            return self.checker.complete(system, user, timeout_s, json_mode)
        return self.reply


# ------------------------------------------------------------- false rejections found in real replies

def test_a_clause_agrees_with_the_matching_half_of_a_but_not_source():
    # Source: "A GP referral is welcome but not required..." Rejected live, although correct.
    src = sources("do I need a referral?")
    n = next(i for i, c in enumerate(src, 1) if "referral" in c.text)
    reply = f"You do not need a GP referral to book a first consultation, although a referral is welcome [S{n}]."
    assert check_reply(reply, src).grounded
    flipped = f"You need a GP referral to book a first consultation [S{n}]."
    assert not check_reply(flipped, src).grounded                       # the real flip is still caught


def test_an_uncited_sentence_the_sources_support_gets_its_citation():
    # Live: the model cited only its closing line, so the supported facts had no marker
    src = sources("we've been trying for a year, what should we do?")
    reply = ("Since you have been trying to conceive for a year, it is usually recommended to seek a fertility "
             "assessment at this point. You may want to contact the clinic team for further guidance and next steps. [S1]")
    g = check_reply(reply, src, patient_text="we've been trying for a year")
    assert g.grounded and g.dropped == 0
    assert g.text.startswith("Since you have been trying") and "assessment at this point. [S1]" in g.text
    assert g.text.endswith("for further guidance and next steps.")      # kept, its wrong marker removed


def test_pointing_to_the_clinic_is_kept_but_never_carries_a_fact_or_number():
    src = sources("what services do you offer?")
    offer = next(n for n, c in enumerate(src, 1) if c.heading == "Services we offer")
    base = f"We offer egg freezing, IVF and IUI. [S{offer}] "
    assert "contact the clinic team" in check_reply(base + "You can contact the clinic team to book.", src).text
    assert "50%" not in check_reply(base + "Contact the clinic team for a 50% discount.", src).text
    alone = check_reply("Please contact the clinic team to discuss your options.", src)
    assert not alone.grounded                                           # a referral alone isn't an answer


def test_the_claim_checker_still_reviews_referral_lines():
    src = sources("what services do you offer?")
    offer = next(n for n, c in enumerate(src, 1) if c.heading == "Services we offer")
    reply = f"We offer egg freezing, IVF and IUI. [S{offer}] You may want to contact the clinic team to discuss your options."
    checker = Checker({1: True, 2: False})
    g = verify_claims(check_reply(reply, src), src, SafeLLM(checker))
    assert "2. You may want to contact the clinic team" in checker.prompts[0]["user"] and "contact" not in g.text


def test_a_referral_line_cannot_smuggle_in_a_promise():
    # Found by testing with the real checker, which passed this line
    src = sources("what services do you offer?")
    offer = next(n for n, c in enumerate(src, 1) if c.heading == "Services we offer")
    g = check_reply(f"We offer egg freezing, IVF and IUI. [S{offer}] "
                    "You can book with the clinic team today and success is guaranteed.", src)
    assert "guaranteed" not in g.text and g.dropped == 1
