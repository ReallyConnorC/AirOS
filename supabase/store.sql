-- Air OS App Store: developers upload .atv apps from the developer website; TVs install and update them.
-- Run once in the Supabase SQL Editor.
create table if not exists public.store_apps (
  id text primary key check (id ~ '^[a-z0-9][a-z0-9.-]{2,63}$'),
  owner uuid not null default auth.uid() references auth.users (id) on delete cascade,
  name text not null check (length(name) between 1 and 40),
  description text not null default '' check (length(description) <= 300),
  version text not null check (version ~ '^[0-9]+(\.[0-9]+){0,3}$'),
  color text,
  icon_url text,
  package_url text not null,
  updated_at timestamptz not null default now()
);
alter table public.store_apps enable row level security;
create policy "anyone can browse the store" on public.store_apps for select using (true);
create policy "developers add their own apps" on public.store_apps for insert to authenticated with check (auth.uid() = owner);
create policy "developers update their own apps" on public.store_apps for update to authenticated using (auth.uid() = owner) with check (auth.uid() = owner);
create policy "developers remove their own apps" on public.store_apps for delete to authenticated using (auth.uid() = owner);

-- App files: apps/<developer id>/<app id>/<version>.atv and icon. Anyone can download; only the developer can write.
insert into storage.buckets (id, name, public, file_size_limit) values ('apps', 'apps', true, 52428800)
on conflict (id) do nothing;
create policy "developers upload into their folder" on storage.objects for insert to authenticated
  with check (bucket_id = 'apps' and (storage.foldername(name))[1] = auth.uid()::text);
create policy "developers replace their files" on storage.objects for update to authenticated
  using (bucket_id = 'apps' and (storage.foldername(name))[1] = auth.uid()::text);
create policy "developers delete their files" on storage.objects for delete to authenticated
  using (bucket_id = 'apps' and (storage.foldername(name))[1] = auth.uid()::text);
create policy "developers see their files" on storage.objects for select to authenticated
  using (bucket_id = 'apps' and (storage.foldername(name))[1] = auth.uid()::text);  -- needed to replace an upload
