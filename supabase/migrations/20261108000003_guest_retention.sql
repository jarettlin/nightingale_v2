-- Guest data retention.
--
-- Unconverted guest sessions are purged 7 days after landing (lead_session.expires_at).
-- 7 days matches the session-recovery window: a guest can resume through their
-- link for exactly as long as we hold what they said, and no longer.
--
-- Destroyed: messages (and their citations), memory items, lead_contact, resume token.
-- Kept: the lead_session row (channel, campaign, stage reached, timestamps) and
-- funnel events. They contain no PHI and answer "where did people drop off?".

create function app.purge_expired_guest_data() returns integer
language plpgsql security definer set search_path = '' as $$
declare
  expired uuid[];
  purged integer;
begin
  select coalesce(array_agg(id), '{}') into expired
  from public.lead_session
  where patient_id is null and purged_at is null and expires_at < now();

  delete from public.memory_item where lead_session_id = any(expired);
  delete from public.message      where lead_session_id = any(expired);  -- citations cascade
  delete from public.lead_contact where lead_session_id = any(expired);

  update public.lead_session
     set purged_at = now(), guest_auth_user_id = null, resume_token_hash = null
   where id = any(expired);
  get diagnostics purged = row_count;

  insert into public.audit_log (actor_type, action, metadata)
  values ('system', 'guest_data_purged', jsonb_build_object('sessions', purged));

  return purged;
end;
$$;

-- Backend/cron only: guests and patients must not trigger deletions
revoke all on function app.purge_expired_guest_data() from public, anon, authenticated;

-- Schedule daily at 03:00 when pg_cron is enabled (Database → Extensions in Supabase)
do $$
begin
  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    perform cron.schedule('purge-expired-guests', '0 3 * * *', 'select app.purge_expired_guest_data()');
  end if;
end $$;
