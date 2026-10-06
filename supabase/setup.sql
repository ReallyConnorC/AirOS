-- Air OS accounts: run this once in your Supabase project (SQL Editor → New query → Run).
-- Each account gets one row holding the settings that follow it between TVs.
create table if not exists public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  settings jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.profiles enable row level security;

-- Signed-in viewers can only see and change their own row.
create policy "read own profile" on public.profiles for select using (auth.uid() = id);
create policy "create own profile" on public.profiles for insert with check (auth.uid() = id);
create policy "update own profile" on public.profiles for update using (auth.uid() = id);
