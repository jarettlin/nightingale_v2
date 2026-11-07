-- Nightingale core schema.
--
-- Conventions
--   * uuid primary keys, text + CHECK instead of enums (easier to evolve)
--   * append-only where history matters: memory_item, consent, audit_log
--   * PHI lives only in: message, memory_item, contact_point, lead_contact,
--     escalation snapshots, clinician_response. Events and audit logs hold IDs only.
--
-- The key idea: messages belong to a lead_session for their whole life.
-- Converting a guest to a patient sets lead_session.patient_id; nothing is
-- copied, so every fact still points at the exact message the guest typed.

-------------------------------------------------------------------------------
-- Clinic and staff
-------------------------------------------------------------------------------

create table public.clinic (
  id                uuid primary key default gen_random_uuid(),
  slug              text not null unique,
  name              text not null,
  emergency_number  text not null,                 -- 999 (MY) / 995 (SG): never hardcoded
  response_window   text not null default '12 to 18 hours',
  timezone          text not null default 'Asia/Kuala_Lumpur',
  created_at        timestamptz not null default now()
);

create table public.staff_member (
  id            uuid primary key default gen_random_uuid(),
  auth_user_id  uuid not null unique references auth.users(id) on delete cascade,
  clinic_id     uuid not null references public.clinic(id),
  role          text not null check (role in ('staff', 'nurse', 'clinician', 'admin')),
  display_name  text not null,
  created_at    timestamptz not null default now()
);

-------------------------------------------------------------------------------
-- Patients and their contact points
-------------------------------------------------------------------------------

create table public.patient (
  -- Immutable internal ID. Never an email or phone, so either can change
  -- without breaking history.
  id            uuid primary key default gen_random_uuid(),
  auth_user_id  uuid unique references auth.users(id) on delete set null,
  clinic_id     uuid not null references public.clinic(id),
  created_at    timestamptz not null default now()
);

create table public.contact_point (
  id           uuid primary key default gen_random_uuid(),
  patient_id   uuid not null references public.patient(id) on delete cascade,
  kind         text not null check (kind in ('email', 'phone', 'instagram', 'tiktok', 'facebook', 'whatsapp')),
  value        text not null,
  verified_at  timestamptz,
  is_primary   boolean not null default false,
  valid_from   timestamptz not null default now(),
  valid_to     timestamptz,                         -- set when replaced; old rows are kept
  created_at   timestamptz not null default now()
);

-- At most one current primary contact of each kind
create unique index contact_point_current_primary
  on public.contact_point (patient_id, kind)
  where is_primary and valid_to is null;

-------------------------------------------------------------------------------
-- Lead sessions: every arrival, guest or not, with its attribution
-------------------------------------------------------------------------------

create table public.lead_session (
  id                  uuid primary key default gen_random_uuid(),
  clinic_id           uuid not null references public.clinic(id),
  guest_auth_user_id  uuid references auth.users(id) on delete set null,  -- Supabase anonymous user
  patient_id          uuid references public.patient(id),                 -- set at conversion

  -- Attribution: must survive to the patient view and every escalation
  source_channel      text not null check (source_channel in (
                        'staff_referral', 'instagram_comment', 'tiktok_comment', 'facebook_comment',
                        'instagram_ad_click', 'google_ad_click', 'lead_form', 'google_reviews',
                        'website_widget', 'direct')),
  campaign_id         text,
  creative            text,
  landing_page        text,
  identity_level      text not null check (identity_level in ('anonymous', 'handle', 'email', 'staff_referred')),
  landing_timestamp   timestamptz not null default now(),
  referral_topic      text,                          -- staff_referral: what the staff member typed
  referred_by         uuid references public.staff_member(id),

  -- Lifecycle
  stage               text not null default 'visitor' check (stage in (
                        'visitor', 'conversation_started', 'value_event', 'auth_started',
                        'consented', 'patient_created', 'escalation_sent')),
  resume_token_hash   text unique,                   -- hash of the token in the resume/referral link
  last_active_at      timestamptz not null default now(),
  converted_at        timestamptz,
  expires_at          timestamptz not null default now() + interval '7 days',
  purged_at           timestamptz,                   -- guest content destroyed, metadata kept
  created_at          timestamptz not null default now(),

  check (source_channel <> 'staff_referral' or referral_topic is not null),
  check ((patient_id is null) = (converted_at is null))
);

create index lead_session_clinic_recent on public.lead_session (clinic_id, last_active_at desc);
create index lead_session_patient on public.lead_session (patient_id);

-- Identity a guest volunteered or we learned from the channel. Kept apart from
-- lead_session so staff can see a lead's metadata without seeing who it is.
create table public.lead_contact (
  lead_session_id   uuid primary key references public.lead_session(id) on delete cascade,
  social_platform   text check (social_platform in ('instagram', 'tiktok', 'facebook')),
  social_handle     text,
  volunteered_email text,
  created_at        timestamptz not null default now()
);

-------------------------------------------------------------------------------
-- Messages
-------------------------------------------------------------------------------

create table public.message (
  id                  uuid primary key default gen_random_uuid(),
  lead_session_id     uuid not null references public.lead_session(id) on delete cascade,
  sender              text not null check (sender in ('patient', 'assistant', 'clinician', 'system')),
  sent_as             text not null check (sent_as in ('guest', 'patient')),  -- said before or after signup
  in_reply_to         uuid references public.message(id),

  content_encrypted   text,                          -- ciphertext of the original, decrypted only by the API
  content_redacted    text,                          -- exactly what the LLM saw; null if redaction failed
  redaction_entities  jsonb not null default '[]',   -- kinds + offsets, never values
  redaction_ok        boolean not null default true,

  -- Risk gate (patient messages)
  risk_level          text check (risk_level in ('low', 'med', 'high')),
  risk_reason         text,
  risk_confidence     text check (risk_confidence in ('low', 'med', 'high')),
  risk_source         text,
  risk_assessed_at    timestamptz,                   -- risk_provenance
  risk_rule_codes     text[] not null default '{}',
  intents             text[] not null default '{}',
  escalation_required boolean,

  -- Assistant messages
  reply_confidence    text check (reply_confidence in ('low', 'med', 'high')),
  advice_generated    boolean,

  -- Voice readiness: a voice note is a message whose text is a transcript
  modality            text not null default 'text' check (modality in ('text', 'voice')),
  audio_object_path   text,                          -- Supabase Storage path (private bucket)
  audio_duration_ms   integer,
  transcript_model    text,
  transcript_confidence real,

  created_at          timestamptz not null default now(),

  check (modality = 'text' or audio_object_path is not null),
  check (sender <> 'patient' or risk_level is not null)
);

create index message_thread on public.message (lead_session_id, created_at);

-------------------------------------------------------------------------------
-- Living memory: append-only facts with provenance
-------------------------------------------------------------------------------

create table public.memory_item (
  id                    uuid primary key default gen_random_uuid(),
  lead_session_id       uuid not null references public.lead_session(id) on delete cascade,
  kind                  text not null check (kind in (
                          'chief_complaint', 'symptom', 'medication', 'allergy',
                          'condition', 'procedure', 'goal', 'other')),
  value                 text not null,
  detail                jsonb not null default '{}',   -- e.g. {"onset": "3 days ago", "dose": "200mg"}
  status                text not null check (status in ('active', 'stopped', 'resolved', 'denied', 'retracted')),
  provenance_message_id uuid not null references public.message(id),
  supersedes_id         uuid unique references public.memory_item(id),  -- a fact is replaced at most once
  conflict_with_id      uuid references public.memory_item(id),
  extracted_by          text not null default 'llm' check (extracted_by in ('llm', 'rule', 'clinician', 'patient_edit')),
  updated_at            timestamptz not null default now()
);

create index memory_item_session on public.memory_item (lead_session_id);

-- The live profile: facts nothing has replaced yet. security_invoker means the
-- caller's RLS applies, so this view can't leak anything the table wouldn't.
create view public.current_memory with (security_invoker = true) as
  select m.*
  from public.memory_item m
  where not exists (select 1 from public.memory_item n where n.supersedes_id = m.id);

-------------------------------------------------------------------------------
-- Citations: which knowledge-base span backs which assistant claim
-------------------------------------------------------------------------------

create table public.citation (
  id              uuid primary key default gen_random_uuid(),
  message_id      uuid not null references public.message(id) on delete cascade,
  marker          text not null,                     -- "S1" as written in the reply
  source_doc      text not null,                     -- e.g. kb/egg-freezing.md
  source_version  text not null,                     -- content hash of the doc when cited
  start_char      integer not null check (start_char >= 0),
  end_char        integer not null,
  check (end_char > start_char)
);

create index citation_message on public.citation (message_id);

-------------------------------------------------------------------------------
-- Consent: append-only. Withdrawal is a new row with granted = false.
-------------------------------------------------------------------------------

create table public.consent (
  id               uuid primary key default gen_random_uuid(),
  clinic_id        uuid not null references public.clinic(id),
  lead_session_id  uuid references public.lead_session(id),
  patient_id       uuid references public.patient(id),
  kind             text not null check (kind in ('data_sharing', 'marketing', 'transactional_email')),
  granted          boolean not null,
  policy_version   text not null,                    -- which wording they agreed to
  recorded_at      timestamptz not null default now(),
  check (lead_session_id is not null or patient_id is not null),
  check (kind <> 'data_sharing' or patient_id is not null)  -- sharing health info needs an account
);

create index consent_patient on public.consent (patient_id, kind, recorded_at desc);
create index consent_lead on public.consent (lead_session_id, kind, recorded_at desc);

-------------------------------------------------------------------------------
-- Escalation to a clinician, and room for the clinician's reply
-------------------------------------------------------------------------------

create table public.escalation (
  id                  uuid primary key default gen_random_uuid(),
  clinic_id           uuid not null references public.clinic(id),
  lead_session_id     uuid not null references public.lead_session(id),
  patient_id          uuid not null references public.patient(id),
  trigger_message_id  uuid not null references public.message(id),
  reason              text not null check (reason in (
                        'high_risk', 'med_risk', 'wants_diagnosis', 'medication_change', 'patient_request')),
  risk_level          text check (risk_level in ('low', 'med', 'high')),
  -- Snapshots are frozen at send time so the clinician sees what the patient saw
  triage_summary      jsonb not null,                -- [{text, provenance_message_ids}], 1–5 items
  profile_snapshot    jsonb not null,                -- current_memory rows incl. provenance ids
  attribution         jsonb not null,                -- copied from lead_session
  status              text not null default 'sent' check (status in (
                        'sent', 'acknowledged', 'in_review', 'responded', 'closed')),
  assigned_to         uuid references public.staff_member(id),
  response_due_at     timestamptz not null,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  check (jsonb_typeof(triage_summary) = 'array'
         and jsonb_array_length(triage_summary) between 1 and 5)
);

create index escalation_queue on public.escalation (clinic_id, status, created_at);

create table public.clinician_response (
  id                  uuid primary key default gen_random_uuid(),
  escalation_id       uuid not null references public.escalation(id) on delete cascade,
  author_id           uuid not null references public.staff_member(id),
  body_encrypted      text not null,
  visible_to_patient  boolean not null default true,
  created_at          timestamptz not null default now()
);

-------------------------------------------------------------------------------
-- Funnel events and audit log: IDs and codes only, never content
-------------------------------------------------------------------------------

create table public.funnel_event (
  id               bigint generated always as identity primary key,
  clinic_id        uuid not null references public.clinic(id),
  lead_session_id  uuid not null references public.lead_session(id) on delete cascade,
  source_channel   text not null,
  event            text not null check (event in (
                     'visitor', 'conversation_started', 'value_event', 'auth_started', 'consented',
                     'patient_created', 'escalation_sent', 'email_captured', 'session_resumed')),
  value_kind       text check (value_kind in (
                     'service_answer', 'articulation_draft', 'question_prep',
                     'conversation_summary', 'live_stat')),
  created_at       timestamptz not null default now(),
  check ((event = 'value_event') = (value_kind is not null))
);

create index funnel_event_clinic on public.funnel_event (clinic_id, created_at);

create table public.audit_log (
  id            bigint generated always as identity primary key,
  actor_type    text not null check (actor_type in ('guest', 'patient', 'staff', 'system')),
  actor_id      uuid,
  action        text not null,
  target_table  text,
  target_id     uuid,
  metadata      jsonb not null default '{}',         -- IDs, counts, codes only
  created_at    timestamptz not null default now(),
  check (length(metadata::text) <= 2000)
);

-------------------------------------------------------------------------------
-- Append-only enforcement. Facts and consents change by adding rows, so the
-- history can't be quietly rewritten. (Guest-data purge may still delete.)
-------------------------------------------------------------------------------

create function public.reject_change() returns trigger
language plpgsql as $$
begin
  raise exception '% is append-only: % is not allowed', tg_table_name, tg_op;
end;
$$;

create trigger audit_log_append_only
  before update or delete on public.audit_log
  for each row execute function public.reject_change();

create trigger memory_item_append_only
  before update on public.memory_item
  for each row execute function public.reject_change();

create trigger consent_append_only
  before update on public.consent
  for each row execute function public.reject_change();
