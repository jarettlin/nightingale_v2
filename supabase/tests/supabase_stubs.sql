-- Minimal stand-ins for what Supabase provides, so migrations run in plain Postgres.
-- Never applied to the real project.
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;

create schema auth;
create table auth.users (
  id uuid primary key,
  is_anonymous boolean not null default false
);

-- Supabase's auth.uid() reads the caller's JWT claims the same way
create function auth.uid() returns uuid
language sql stable as $$
  select nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'sub', '')::uuid
$$;

grant usage on schema auth to anon, authenticated;
grant execute on function auth.uid() to anon, authenticated;
