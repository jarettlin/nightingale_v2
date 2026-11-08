# Nightingale PWA

Patient messenger for the Nightingale API: React + TypeScript + Vite, installable as a PWA.

## Run

1. Start the API (from `backend/`): `.venv/bin/uvicorn app.main:app --port 8000`
2. Copy `.env.example` to `.env.local` and fill in the Supabase URL and **publishable** key.
3. `npm install`, then `npm run dev` and open http://localhost:5173/?source=instagram&campaign=ivf_over40

In development, `/api/*` is proxied to `127.0.0.1:8000`, so there is no CORS setup.

## Staff console (`/staff`)

Staff accounts are **invite-only**: the `/staff` page never creates accounts, and it gives the same answer
for any email so it can't be used to find out who works at the clinic. An admin invites people from
`backend/` (needs `SUPABASE_SECRET_KEY` in `backend/.env`):

    .venv/bin/python scripts/add_staff.py add nurse@clinic.example nurse "Nurse Tan"
    .venv/bin/python scripts/add_staff.py remove nurse@clinic.example
    .venv/bin/python scripts/add_staff.py list

The invite email signs them in at `/staff`; after that they use "Email me a sign-in link". Nurses and
clinicians review and reply; front desk (`staff`) can acknowledge. Every rule is enforced by the API.

## Demo mode (no email, no Supabase, no OpenAI)

For trying the console or recording a demo: stop the real API, then from `backend/` run
`.venv/bin/python scripts/demo_server.py`. It serves synthetic patients and escalations in memory and prints
sign-in links for a nurse, front desk and a patient. The links only work with `npm run dev`: the production
build compiles the demo sign-in out.

## What's here

- `src/App.tsx`: the thread. Guests are signed in anonymously and get answers before any sign-up. The last
  conversation is resumed on reload, and `?source=` / `?campaign=` attribute new sessions.
- `src/components/MessageBubble.tsx`: replies with tappable source numbers that show the clinic's own text.
- `src/components/ProfileDrawer.tsx`: "What you've shared", the live profile. Tap a fact to jump to the message it came from.
- `src/staff/`: the console. A risk-ordered queue, a review screen showing the patient's own words behind
  every triage bullet and fact, attribution, status actions, replies, and consented patients' threads.
- `src/components/ContinueSecurely.tsx`: email verification (upgrades the anonymous user in place), then phone,
  a list of what will be shared with per-item opt-out, and separate data-sharing and marketing consent.

The service worker caches the app shell only. Conversations are never stored offline.
