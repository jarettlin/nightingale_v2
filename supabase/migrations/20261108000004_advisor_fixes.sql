-- Fixes flagged by the Supabase security and performance advisors.

-- Security (function_search_path_mutable): pin search_path so the trigger
-- function can't be steered by a caller's search_path. Body is unchanged.
create or replace function public.reject_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception '% is append-only: % is not allowed', tg_table_name, tg_op;
end;
$$;

-- Performance (auth_rls_initplan): wrap auth.uid() in a subselect so Postgres
-- evaluates it once per query instead of once per row. Same rows visible.
alter policy staff_self_and_colleagues on public.staff_member
  using (auth_user_id = (select auth.uid()) or clinic_id = app.staff_clinic_id());
