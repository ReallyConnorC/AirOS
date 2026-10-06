-- Air OS: sign in on a TV by scanning a QR code with a phone. Run once in the Supabase SQL Editor.
-- The TV creates a one-time code; the phone signs in on the web page and hands its session to that code;
-- the TV collects it. Nobody can read the table directly, only through these three functions.
create table if not exists public.tv_pairings (
  code text primary key,
  refresh_token text,
  created_at timestamptz not null default now()
);
alter table public.tv_pairings enable row level security;  -- no policies: only the functions below touch it

create or replace function public.create_pair(p_code text) returns void
language plpgsql security definer set search_path = public as $$
begin
  if length(p_code) < 16 then raise exception 'code too short'; end if;
  delete from tv_pairings where created_at < now() - interval '10 minutes';
  insert into tv_pairings (code) values (p_code);
end $$;

create or replace function public.complete_pair(p_code text, p_token text) returns boolean
language plpgsql security definer set search_path = public as $$
begin
  update tv_pairings set refresh_token = p_token
   where code = p_code and refresh_token is null and created_at > now() - interval '10 minutes';
  return found;
end $$;

create or replace function public.claim_pair(p_code text) returns text
language plpgsql security definer set search_path = public as $$
declare t text;
begin
  delete from tv_pairings where code = p_code and refresh_token is not null returning refresh_token into t;
  return t;
end $$;

revoke all on function public.create_pair(text), public.complete_pair(text, text), public.claim_pair(text) from public;
grant execute on function public.create_pair(text), public.complete_pair(text, text), public.claim_pair(text) to anon, authenticated;
