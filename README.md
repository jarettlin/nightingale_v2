# Nightingale

A first-touch-to-care PWA for a fertility clinic. A stranger arrives from an ad, a comment, a staff referral
or a lead form and gets real, cited answers **before** signing up. When they're ready, they continue securely,
and everything they said comes with them. A risk gate catches emergencies, living memory keeps a structured
profile with provenance, and **Send to Clinic** hands a nurse a complete, traceable case.

All data and clinic content are synthetic. See [docs/TECHNICAL_BRIEF.md](docs/TECHNICAL_BRIEF.md) for the
design and [ATTRIBUTION.txt](ATTRIBUTION.txt) for libraries and licenses.

```
backend/      FastAPI app, pipeline, tests            frontend/   React PWA: patient chat at /, staff console at /staff
  app/        redaction, risk, memory, grounding…     supabase/   migrations (schema + RLS), seed, PGlite access checks
  kb/         clinic knowledge base (markdown)        docs/       technical brief
  channel_rules.yaml  value_content.yaml  evals/risk_cases.jsonl
```

## Setup

You need Python 3.12+, Node 20+, a Supabase project, and optionally an OpenAI API key. Without one, the app
runs in safe fallback mode: rules-only risk checks and templated replies.

**1. Supabase**
- Run `supabase/migrations/*.sql` in order (SQL editor or Supabase MCP), then `supabase/seed.sql`.
- Authentication → Sign In / Providers: turn on **Anonymous sign-ins**.
- Authentication → URL Configuration: Site URL `http://localhost:5173`, redirect URL `http://localhost:5173/**`.
- Optional: enable **pg_cron** so the daily guest-data purge is scheduled.

**2. Backend**
```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill it in; every variable is explained in the file
```
Required: `DATABASE_URL` (Session pooler URI), `MESSAGE_ENCRYPTION_KEY`, `SUPABASE_URL`. Recommended:
`OPENAI_API_KEY`. Optional: `SUPABASE_SECRET_KEY` (only for inviting staff), `META_APP_SECRET` and
`META_VERIFY_TOKEN` (comment webhook), `LEAD_FORM_SECRET`.

**3. Frontend**
```bash
cd frontend && npm install && cp .env.example .env.local   # Supabase URL + publishable key only
```

## Run

```bash
cd backend && .venv/bin/uvicorn app.main:app --port 8000
cd frontend && npm run dev
```

- Patient: http://localhost:5173/?source=instagram&campaign=ivf_over40
- Staff: http://localhost:5173/staff. Staff accounts are invite-only:
  `.venv/bin/python scripts/add_staff.py add nurse@clinic.example nurse "Nurse Tan"`
- **Demo mode** (no email, Supabase or OpenAI needed): stop the API, then run
  `.venv/bin/python scripts/demo_server.py`. It serves synthetic patients and escalations and prints sign-in
  links for a nurse, front desk, a patient and a new visitor. The links only work with `npm run dev`.

## Tests

```bash
cd backend && .venv/bin/pytest -q                 # 283 tests; Supabase ones run when DATABASE_URL is set
cd supabase/tests && npm install && npm run check # RLS policies on an in-memory Postgres (32 checks)
cd backend && .venv/bin/python scripts/eval_risk.py --runs 3   # risk gate vs 52 labelled messages (uses OpenAI)
cd backend && .venv/bin/python scripts/smoke_auth.py           # real Supabase anonymous sign-in against a running API
```

The brief's required tests:

| Required test | File |
|---|---|
| guest → patient conversion | `tests/test_guest_to_patient_conversion.py` |
| value events | `tests/test_value_events.py` |
| escalation payload | `tests/test_escalation_payload.py` |
| risk escalation | `tests/test_risk_escalation.py` |
| memory mutation | `tests/test_memory_mutation.py` |
| redaction | `tests/test_redaction.py` |
| access control | `tests/test_access_control.py` |
| trust ("are you a real doctor?") | `tests/test_trust.py` |

Further tests cover grounding (citations resolve to real file spans), channels (rules config, staff referral,
comment webhook, lead form, funnel, warm leads), latency, the OpenAI client's failure handling, the demo seed,
and a Supabase round trip of every feature.

## Where redaction happens

- **[`app/redaction.py`](backend/app/redaction.py)**: removes names (from cues like "my name is…", greetings,
  honorifics and patronymics), Malaysian IC numbers, Singapore NRIC/FIN numbers, passport numbers, phone numbers,
  emails, social handles and full dates. It also always removes the person's own stored identifiers. Symptoms
  and medicines are kept. A second scan checks the output, and if anything identifier-shaped survives, or
  anything fails, it **fails closed**.
- **[`app/llm.py`](backend/app/llm.py) `SafeLLM`** is the only route to a model. It redacts every prompt first
  and refuses to call the model if redaction fails.
- **Storage:** each message keeps an encrypted original (`crypto.py`) and the redacted copy the model saw.
  Retrieval runs on the redacted text. Memory values are re-checked so they can never hold an identifier.
- **Outputs:** the shareable note and summary email contain no patient text at all, and are checked again
  before sending.
- **Logs and audit** ([`app/logs.py`](backend/app/logs.py)) hold only IDs, counts and codes. Long strings are
  dropped as a backstop.

## How access control is enforced

1. **Every request needs a Supabase JWT,** checked server-side against the project's published signing key
   ([`app/auth.py`](backend/app/auth.py)).
2. **Roles come from our own tables** (`staff_member`, `patient`), never from token claims. A guest is a
   Supabase anonymous user.
3. **Ownership is checked on every route** ([`app/api.py`](backend/app/api.py)). A conversation that isn't
   yours returns **404**, so IDs can't be probed. Staff routes return **403** for non-staff, are scoped to one
   clinic, and show patient content only while the patient's **current** consent allows it.
4. **Inside the database,** RLS ([`supabase/migrations/…_rls.sql`](supabase/migrations)) enforces the same
   rules for anyone using a browser token. Clients have **no write grants**: only the backend writes, after
   redaction and the risk gate.
5. **Staff accounts are invite-only.** `/staff` never creates accounts, and roles are granted and revoked with
   `scripts/add_staff.py`.

## Failure modes

| What fails | What happens |
|---|---|
| Model times out or errors | Each call has a short timeout and one retry. Risk falls back to the rules with a **Medium** floor. Replies become an honest "can't answer reliably, send it to the clinic?". Extraction falls back to the offline rules. |
| Model returns junk or unsupported claims | JSON is validated. Grounding drops any sentence with a fake citation, an invented number or a meaning that contradicts its source, and if nothing cited survives, the reply is "I'd rather not guess". |
| Redaction fails | The model is never called. The patient gets a safe template. The original is still stored encrypted. |
| Supabase Auth unreachable | **503** with the emergency number. No token is trusted without a verified signature. |
| Database down | The request fails (500). The patient sees "didn't send, try again", and the emergency notice stays on screen. |
| Encryption key missing | The server refuses to start, so plaintext is never stored "temporarily". |
| Abuse | Guests are limited to 10 messages a minute and 100 a day, 10 new sessions an hour and 3 summary emails an hour. Inputs are length-capped. Comment replies go out at most once per person per day. |
| Email or Meta sending | Both go to an outbox until a provider or reviewed Meta app is connected. The outbox is the honest state of this build. |

**Retention:** unconverted guest conversations are deleted after 7 days by a daily pg_cron job. That's the
same window as session recovery. Channel, campaign and funnel stage are kept, because they're PHI-free and
used for abandonment analytics.
