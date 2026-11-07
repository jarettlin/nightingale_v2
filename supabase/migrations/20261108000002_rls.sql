-- Row-level security.
--
-- Model
--   * Reads: browsers may read through Supabase with their own JWT; RLS decides
--     which rows they get. FastAPI reads the same way (it forwards the user's
--     JWT), so a bug in Python still can't show Patient A's rows to Patient B.
--   * Writes: only the backend writes, after redaction and the risk gate.
--     anon/authenticated have no insert/update/delete rights at all, so a
--     patient can't forge an assistant message or set their own risk level.
--
-- Who is who
--   * guest   = Supabase anonymous user, owns lead_session.guest_auth_user_id
--   * patient = patient.auth_user_id; converting an anonymous user keeps the
--               same auth uid, so the guest's session stays theirs
--   * staff   = staff_member.auth_user_id, scoped to one clinic, and sees
--               message content only after the patient consented to sharing

create schema if not exists app;
grant usage on schema app to anon, authenticated;

-------------------------------------------------------------------------------
-- Helpers. SECURITY DEFINER so policies can look things up without tripping
-- RLS recursively; each returns facts about the caller only.
-------------------------------------------------------------------------------

create function app.current_patient_id() returns uuid
language sql stable security definer set search_path = '' as $$
  select id from public.patient where auth_user_id = auth.uid()
$$;

create function app.staff_clinic_id() returns uuid
language sql stable security definer set search_path = '' as $$
  select clinic_id from public.staff_member where auth_user_id = auth.uid()
$$;

-- Latest data_sharing decision for this patient wins (withdrawal = newer row)
create function app.patient_consented(p_patient_id uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select coalesce((
    select c.granted from public.consent c
    where c.patient_id = p_patient_id and c.kind = 'data_sharing'
    order by c.recorded_at desc
    limit 1
  ), false)
$$;

create function app.owns_lead_session(p_lead_session_id uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.lead_session l
    where l.id = p_lead_session_id
      and (l.guest_auth_user_id = auth.uid()
           or (l.patient_id is not null and l.patient_id = app.current_patient_id()))
  )
$$;

-- Staff may see this session's content: same clinic, converted, consented
create function app.staff_can_read_session(p_lead_session_id uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.lead_session l
    where l.id = p_lead_session_id
      and l.clinic_id = app.staff_clinic_id()
      and l.patient_id is not null
      and app.patient_consented(l.patient_id)
  )
$$;

revoke all on all functions in schema app from public;
grant execute on all functions in schema app to anon, authenticated;

-------------------------------------------------------------------------------
-- Lock everything down, then grant reads back table by table
-------------------------------------------------------------------------------

do $$
declare t text;
begin
  foreach t in array array[
    'clinic', 'staff_member', 'patient', 'contact_point', 'lead_session', 'lead_contact',
    'message', 'memory_item', 'citation', 'consent', 'escalation', 'clinician_response',
    'funnel_event', 'audit_log'
  ] loop
    -- Not FORCE: the table owner (backend + helper functions) must bypass RLS
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on public.%I from anon, authenticated', t);
  end loop;
end $$;

revoke all on public.current_memory from anon, authenticated;

grant select on public.clinic to anon, authenticated;
grant select on public.staff_member, public.patient, public.contact_point, public.lead_session,
                public.lead_contact, public.message, public.memory_item, public.current_memory,
                public.citation, public.consent, public.escalation, public.clinician_response,
                public.funnel_event
  to authenticated;
-- audit_log: no grants. Only the backend reads it.

-------------------------------------------------------------------------------
-- Policies (SELECT only; there are deliberately no write policies)
-------------------------------------------------------------------------------

-- Public clinic info (name, hours, emergency number)
create policy clinic_public on public.clinic
  for select to anon, authenticated using (true);

create policy staff_self_and_colleagues on public.staff_member
  for select to authenticated
  using (auth_user_id = auth.uid() or clinic_id = app.staff_clinic_id());

create policy patient_self on public.patient
  for select to authenticated
  using (id = app.current_patient_id());

create policy patient_staff on public.patient
  for select to authenticated
  using (clinic_id = app.staff_clinic_id() and app.patient_consented(id));

create policy contact_point_self on public.contact_point
  for select to authenticated
  using (patient_id = app.current_patient_id());

create policy contact_point_staff on public.contact_point
  for select to authenticated
  using (exists (select 1 from public.patient p
                 where p.id = patient_id
                   and p.clinic_id = app.staff_clinic_id()
                   and app.patient_consented(p.id)));

-- Owners see their sessions. Staff see every lead in their clinic, because the
-- warm-lead view needs channel/stage/recency; these columns hold no PHI.
create policy lead_session_owner on public.lead_session
  for select to authenticated
  using (app.owns_lead_session(id));

create policy lead_session_staff on public.lead_session
  for select to authenticated
  using (clinic_id = app.staff_clinic_id());

-- Who the lead is: staff only once there's consent to be contacted
create policy lead_contact_owner on public.lead_contact
  for select to authenticated
  using (app.owns_lead_session(lead_session_id));

create policy lead_contact_staff on public.lead_contact
  for select to authenticated
  using (
    app.staff_can_read_session(lead_session_id)
    or (exists (select 1 from public.lead_session l
                where l.id = lead_session_id and l.clinic_id = app.staff_clinic_id())
        and coalesce((select c.granted from public.consent c
                      where c.lead_session_id = lead_contact.lead_session_id and c.kind = 'marketing'
                      order by c.recorded_at desc limit 1), false))
  );

create policy message_owner on public.message
  for select to authenticated
  using (app.owns_lead_session(lead_session_id));

create policy message_staff on public.message
  for select to authenticated
  using (app.staff_can_read_session(lead_session_id));

create policy memory_owner on public.memory_item
  for select to authenticated
  using (app.owns_lead_session(lead_session_id));

create policy memory_staff on public.memory_item
  for select to authenticated
  using (app.staff_can_read_session(lead_session_id));

-- A citation is visible exactly when its message is (message RLS applies inside)
create policy citation_via_message on public.citation
  for select to authenticated
  using (exists (select 1 from public.message m where m.id = message_id));

create policy consent_owner on public.consent
  for select to authenticated
  using (patient_id = app.current_patient_id()
         or (lead_session_id is not null and app.owns_lead_session(lead_session_id)));

create policy consent_staff on public.consent
  for select to authenticated
  using (clinic_id = app.staff_clinic_id());

create policy escalation_owner on public.escalation
  for select to authenticated
  using (patient_id = app.current_patient_id());

create policy escalation_staff on public.escalation
  for select to authenticated
  using (clinic_id = app.staff_clinic_id() and app.patient_consented(patient_id));

create policy clinician_response_owner on public.clinician_response
  for select to authenticated
  using (visible_to_patient and exists (
    select 1 from public.escalation e
    where e.id = escalation_id and e.patient_id = app.current_patient_id()));

create policy clinician_response_staff on public.clinician_response
  for select to authenticated
  using (exists (select 1 from public.escalation e
                 where e.id = escalation_id and e.clinic_id = app.staff_clinic_id()));

create policy funnel_event_staff on public.funnel_event
  for select to authenticated
  using (clinic_id = app.staff_clinic_id());
