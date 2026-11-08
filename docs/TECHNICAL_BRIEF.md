# Nightingale: Technical Brief

**Goal:** catch a prospect at the moment of interest, help them before asking for anything, and hand a nurse a
case they can act on without the patient repeating themselves. All data is synthetic.

## 1. Architecture

```
 Instagram/FB/TikTok comment ─┐  signed webhook → prepared session + one private reply (/r/<token>)
 Staff referral note ─────────┤  staff console → personal link, topic pre-loaded
 Lead form ───────────────────┤  secret webhook → email prefilled, marketing consent recorded
 Ad / review / website link ──┘  ?source=&campaign= → session with attribution
                │
                ▼
 PWA (React) ── Supabase Auth (anonymous guest → same user id after email verification)
                │  JWT on every request
                ▼
 FastAPI ── per message: redact → risk gate (rules, then model may only RAISE) ─┬─ escalate: safety template, no advice call
                │                                                                └─ low: retrieve clinic docs → grounded reply
                │                                                                       → citation rules → model claim check (can only remove)
                │   in parallel: fact extraction → validated, append-only memory
                ▼
 Supabase Postgres: RLS on every table, also applied to the backend's own reads (read_as);
                    clients cannot write; pg_cron purges guest data after 7 days
 OpenAI gpt-4.1-mini: sees redacted text only, through one gateway (SafeLLM)
```

Choices kept the moving parts minimal: one API process, one database, one model provider, rules in YAML files
(`channel_rules.yaml`, `value_content.yaml`) and clinic knowledge in markdown (`kb/`).

## 2. Data schema: how the parts link

```
lead_session (attribution: channel, campaign, creative, identity_level, topic) ──┬── lead_contact (handle/email, hidden until consent)
   │ patient_id set at conversion; nothing is copied                              └── consent (append-only, per kind, timestamped)
   ▼
message (encrypted original, redacted copy, risk_level/reason/confidence/assessed_at, audio fields)
   ├── citation (marker → kb file, version hash, exact start/end character span)
   └── memory_item (value, status, provenance_message_id → message, supersedes_id → previous version)
            └── current_memory view = items nothing supersedes = the live profile
escalation (trigger_message_id, triage_summary[{text, provenance_message_ids}], profile_snapshot, attribution, status)
   └── clinician_response (encrypted body, visible_to_patient)
funnel_event, audit_log: IDs and codes only
```

**Linking, not copying, is the core decision.** Converting a guest just sets `lead_session.patient_id`, so a
fact first stated as a guest still points at that exact guest message, and attribution survives to every
escalation. A correction ("I stopped Advil") adds a new memory row whose `supersedes_id` points to the old
one: an unbroken provenance chain. A contradiction without a correction is flagged (`conflict_with_id`)
for a clinician to resolve, never settled by the model. **A clinician module attaches through
`escalation.status`** (sent → acknowledged → in_review → responded → closed) and `clinician_response`. The
review packet resolves every triage bullet and profile fact back to the patient's original words. That's
how the staff console works today, and an EHR integration would read the same records.

## 3. Channels and ethics

Rated on four axes: **T** technically possible · **L** legal under Malaysia's PDPA 2010 (health data is
*sensitive personal data*, needing explicit consent) and medical-advertising rules (MAB / private healthcare
advertising regulations) · **P** platform policy · **R** trust-compatible. This is not legal advice: the
ratings flag where a lawyer should be consulted.

| Channel | T | L | P | R | Status |
|---|---|---|---|---|---|
| Staff referral link (topic typed by staff, names redacted) | 🟢 | 🟢 | 🟢 | 🟢 | **Built** |
| Comment on clinic's own post → one private reply with link | 🟢 | 🟢 | 🟢 | 🟢 | **Built** (outbox until Meta app review; comment text never stored) |
| Ad click / Google review reply link / website widget | 🟢 | 🟢 | 🟢 | 🟢 | **Built** (ads themselves must follow MAB rules) |
| Lead form (identified, separate marketing checkbox) | 🟢 | 🟢 | 🟢 | 🟢 | **Built** |
| QR code at the clinic or community health talks (new) | 🟢 | 🟢 | 🟢 | 🟢 | Works today: it's a staff-referral-style link |
| WhatsApp click-to-chat from the clinic's own number | 🟢 | 🟡 | 🟢 | 🟢 | Yellow: health data on a third-party messenger. Improve: hand off to the PWA after the first message. |
| Partner pharmacy / GP referral (new) | 🟢 | 🟡 | 🟢 | 🟢 | Yellow: needs a data-sharing agreement. Improve: patient-initiated link, no data passed. |
| DM triggered by a **like** | 🔴 | 🟡 | 🔴 | 🔴 | **Not built.** No webhook exists for likes, and messaging someone for liking a fertility post feels like surveillance. |
| Scraping competitors' reviews to contact reviewers | 🟢 | 🔴 | 🔴 | 🔴 | **Not built.** No consent, and against platform terms. |
| DMing people in public health threads/groups | 🟢 | 🔴 | 🔴 | 🔴 | **Not built.** Targets inferred health status. |
| Condition-based retargeting / lookalikes from patient lists | 🟢 | 🔴 | 🔴 | 🔴 | **Not built.** Processes sensitive data for marketing without explicit consent. |

## 4. Assumptions we challenge

1. **"Respond in 12–18 hours" is unsafe for high risk.** For crushing chest pain or heavy bleeding, the
   patient is told to call 999 now and *not to wait*. The escalation is still logged, due in 1 hour, and
   placed at the top of a queue ordered by risk.
2. **The emergency number is configuration, not code.** It's 999 in Malaysia and 995 in Singapore, and the
   brief spans both.
3. **A statistic isn't a value event.** The live count ("N people asked this week") is tracked and audited,
   but it isn't counted as help. Our first real count also exposed an honesty bug: it counted
   *conversations*, so two testers showed as "10 people". It now counts distinct people, and it's hidden
   below 5.
4. **The model must never lower risk.** Rules run first, and the model's opinion can only raise the level.
   When the model is unavailable, risk is floored at Medium.
5. **Deleting guest data vs session recovery:** we chose one rule for both: a 7-day window.
6. **Staff sign-up must not be open.** Staff accounts are invite-only, and the login page gives the same
   answer for any email.

## 5. Measured, not assumed

- **Risk gate** (52 labelled messages × 3 runs, gpt-4.1-mini): **48/48** emergencies caught, **36/36**
  ambiguous messages reached a clinician, and **66/72** routine messages were answered (up from 35/48 before
  the levels were defined). Rules alone miss 3 emergencies (Malay phrasing, an indirect overdose, a possible
  ectopic pregnancy), which is why the model's second opinion matters.
- **Grounding:** made-up services, prices and durations, fake citations, and "we offer X" when the clinic
  doesn't are all dropped (tested on real OpenAI output). Every citation resolves to an exact character span
  of a versioned clinic file. The model claim check then catches what the rules can't see: "most patients
  find the injections painless" shares almost every word with its source and passes the rules, but the
  checker drops it. It kept every correct sentence across 5 repeats and 10 real questions, for 0.8–1.4 s
  extra per answer.
- **Latency:** about 5.4 s per message end to end in the live app: roughly 3.1 s of model calls (risk,
  reply, claim check) and roughly 2.3 s of database round trips. Fact extraction runs in parallel and may
  hold a finished reply for at most 1.5 s; seen live, it stalled for 7.3 s and the reply went out anyway,
  with the facts saved by the offline rules. The risk check is never skipped, so a stalled risk call
  (6.7 s once, on the first call after a restart) still delays the reply. Before per-call timeouts, one
  stalled call took 18 s.
- **Tests:** 296 backend tests, including a Supabase round trip of every feature, plus 32 RLS checks on
  PGlite.

## 6. Trade-offs and what we cut

- **Grounding is rules plus a model claim check, not a trained NLI model.** The checker uses the same
  model family as the reply, so it can share blind spots; it can only remove sentences, and if it is down
  the rule-checked reply stands with confidence capped at Medium. The trade-off is about one extra second
  per answer.
- **Keyword retrieval (BM25), not embeddings.** It's predictable and testable for 8 documents; a larger
  knowledge base would need embeddings.
- **Outbox senders for email and Meta.** Real sending needs a provider and a reviewed Meta app; the interfaces
  are in place.
- **Two locks on every read of patient content.** The API checks ownership, clinic and consent in Python,
  and the read itself runs as that signed-in user (`read_as`), so RLS filters it again. Writes, the audit
  log and staff-wide funnel counts still run as the backend role, by design.
- **Single clinic, in-memory rate limits, localhost deployment.** Multi-instance would need Redis or Postgres
  limits.
- **Not built:** real Meta integration, dormant-lead recall, voice.

## 7. Voice

The schema is ready: every `message` has `modality`, `audio_object_path` (private Storage bucket),
`audio_duration_ms`, `transcript_model` and `transcript_confidence`. A voice note becomes a message whose text
is its transcript, and then flows through the same pipeline: redact → risk → grounded reply → memory, with
provenance pointing at the audio message. **Low transcription confidence lowers risk confidence and floors
risk at Medium**, the same rule as a model outage. Audio is encrypted at rest, follows the same retention as
text, and never reaches the language model (only the redacted transcript does). Next steps would be
segment-level offsets so a fact can cite the second it was said, and spoken replies limited to the same
grounded text.
