-- Air Family: run this migration in the existing Air OS Supabase SQL editor.
-- No service-role key is used by the website, APK or TV. Writes go through checked RPCs.
begin;
create table if not exists public.family_homes (
 id uuid primary key default gen_random_uuid(), owner uuid not null references auth.users(id),
 name text not null check(length(name) between 1 and 40),
 invite_code text not null unique default replace(gen_random_uuid()::text,'-',''),
 kitchen_code text not null unique default replace(gen_random_uuid()::text,'-',''), created_at timestamptz not null default now());
create table if not exists public.family_members (
 home_id uuid references public.family_homes(id) on delete cascade, user_id uuid references auth.users(id) on delete cascade,
 display_name text not null check(length(display_name) between 1 and 32), role text not null check(role in ('owner','kitchen','member')),
 primary key(home_id,user_id));
create table if not exists public.family_items (
 id uuid primary key default gen_random_uuid(), home_id uuid not null references public.family_homes(id) on delete cascade,
 name text not null check(length(name) between 1 and 60), kind text not null check(kind in ('food','drink')),
 quantity integer not null check(quantity between 0 and 100000), active boolean not null default true);
create unique index if not exists family_item_name on public.family_items(home_id,lower(name));
create table if not exists public.family_orders (
 id uuid primary key default gen_random_uuid(), home_id uuid not null references public.family_homes(id) on delete cascade,
 user_id uuid not null references auth.users(id), display_name text not null, items jsonb not null,
 client_id uuid not null, status text not null default 'pending' check(status in ('pending','ready','delivered','cancelled')),
 created_at timestamptz not null default now(), updated_at timestamptz not null default now(), unique(home_id,user_id,client_id));
create table if not exists public.family_messages (
 id uuid primary key default gen_random_uuid(), home_id uuid not null references public.family_homes(id) on delete cascade,
 user_id uuid not null references auth.users(id), display_name text not null,
 text text not null check(length(text) between 1 and 350), client_id uuid not null,
 created_at timestamptz not null default now(), claimed_at timestamptz, claimed_by uuid, announced_at timestamptz,
 unique(home_id,user_id,client_id));
create index if not exists family_order_recent on public.family_orders(home_id,created_at desc);
create index if not exists family_message_queue on public.family_messages(home_id,created_at) where announced_at is null;

create or replace function public.family_role(p_home uuid) returns text
language sql stable security definer set search_path = '' as $$
 select role from public.family_members where home_id=p_home and user_id=auth.uid()
$$;
alter table public.family_homes enable row level security;
alter table public.family_members enable row level security;
alter table public.family_items enable row level security;
alter table public.family_orders enable row level security;
alter table public.family_messages enable row level security;
drop policy if exists family_home_read on public.family_homes;
create policy family_home_read on public.family_homes for select to authenticated using(owner=auth.uid());
drop policy if exists family_member_read on public.family_members;
create policy family_member_read on public.family_members for select to authenticated using(user_id=auth.uid() or public.family_role(home_id) in ('owner','kitchen'));
drop policy if exists family_item_read on public.family_items;
create policy family_item_read on public.family_items for select to authenticated using(public.family_role(home_id) is not null);
drop policy if exists family_order_read on public.family_orders;
create policy family_order_read on public.family_orders for select to authenticated using(public.family_role(home_id) is not null and (user_id=auth.uid() or public.family_role(home_id) in ('owner','kitchen')));
drop policy if exists family_message_read on public.family_messages;
create policy family_message_read on public.family_messages for select to authenticated using(public.family_role(home_id) is not null and (user_id=auth.uid() or public.family_role(home_id) in ('owner','kitchen')));
revoke all on public.family_homes,public.family_members,public.family_items,public.family_orders,public.family_messages from anon,authenticated;
grant select on public.family_homes,public.family_members,public.family_items,public.family_orders,public.family_messages to authenticated;

create or replace function public.family_me() returns jsonb
language sql stable security definer set search_path = '' as $$
 select coalesce(jsonb_agg(jsonb_build_object('id',h.id,'name',h.name,'display_name',m.display_name,'role',m.role,
 'invite_code',case when m.role in ('owner','kitchen') then h.invite_code end,
 'kitchen_code',case when m.role='owner' then h.kitchen_code end) order by h.created_at),'[]'::jsonb)
 from public.family_members m join public.family_homes h on h.id=m.home_id where m.user_id=auth.uid()
$$;
create or replace function public.family_create(p_name text,p_display_name text) returns uuid
language plpgsql security definer set search_path = '' as $$
declare h uuid;
begin
 if auth.uid() is null then raise exception 'Sign in first'; end if;
 if (select count(*) from public.family_homes where owner=auth.uid())>=5 then raise exception 'You already have five households'; end if;
 insert into public.family_homes(owner,name) values(auth.uid(),trim(p_name)) returning id into h;
 insert into public.family_members values(h,auth.uid(),trim(p_display_name),'owner'); return h;
end $$;
create or replace function public.family_join(p_code text,p_display_name text) returns uuid
language plpgsql security definer set search_path = '' as $$
declare h public.family_homes; r text;
begin
 if auth.uid() is null then raise exception 'Sign in first'; end if;
 select * into h from public.family_homes where invite_code=trim(p_code) or kitchen_code=trim(p_code);
 if h.id is null then raise exception 'Household code not found'; end if;
 r=case when h.kitchen_code=trim(p_code) then 'kitchen' else 'member' end;
 insert into public.family_members values(h.id,auth.uid(),trim(p_display_name),r)
 on conflict(home_id,user_id) do update set display_name=excluded.display_name,
 role=case when family_members.role='owner' then 'owner' when excluded.role='kitchen' then 'kitchen' else family_members.role end;
 return h.id;
end $$;
create or replace function public.family_name(p_home uuid,p_name text) returns void
language plpgsql security definer set search_path = '' as $$
begin
 update public.family_members set display_name=trim(p_name) where home_id=p_home and user_id=auth.uid();
 if not found then raise exception 'Join this household first'; end if;
end $$;
create or replace function public.family_item_save(p_home uuid,p_id uuid,p_name text,p_kind text,p_quantity integer,p_active boolean default true) returns uuid
language plpgsql security definer set search_path = '' as $$
declare i uuid;
begin
 if coalesce(public.family_role(p_home),'') not in ('owner','kitchen') then raise exception 'Kitchen access required'; end if;
 if p_id is null then
  insert into public.family_items(home_id,name,kind,quantity,active) values(p_home,trim(p_name),p_kind,p_quantity,p_active) returning id into i;
 else
  update public.family_items set name=trim(p_name),kind=p_kind,quantity=p_quantity,active=p_active where id=p_id and home_id=p_home returning id into i;
  if i is null then raise exception 'Item not found'; end if;
 end if; return i;
end $$;
create or replace function public.family_order(p_home uuid,p_items jsonb,p_client uuid) returns uuid
language plpgsql security definer set search_path = '' as $$
declare m public.family_members; v record; i public.family_items; snapshot jsonb='[]'; o uuid;
begin
 select * into m from public.family_members where home_id=p_home and user_id=auth.uid() for update;
 if m.user_id is null then raise exception 'Join this household first'; end if;
 select id into o from public.family_orders where home_id=p_home and user_id=auth.uid() and client_id=p_client;
 if o is not null then return o; end if;
 if p_items is null or jsonb_typeof(p_items)<>'array' or jsonb_array_length(p_items) not between 1 and 10 then raise exception 'Choose 1 to 10 items'; end if;
 if (select count(*) from public.family_orders where user_id=auth.uid() and created_at>now()-interval '1 minute')>=5 then raise exception 'Please wait before sending another request'; end if;
 for v in select (value->>'id')::uuid as id,sum((value->>'quantity')::integer)::integer as quantity
  from jsonb_array_elements(p_items) group by (value->>'id')::uuid order by (value->>'id')::uuid loop
  if v.quantity is null or v.quantity not between 1 and 20 then raise exception 'Choose quantities between 1 and 20'; end if;
  -- Validate individual values as well as their sum, rejecting negative duplicate entries.
  if exists(select 1 from jsonb_array_elements(p_items) e where (e->>'id')::uuid=v.id and ((e->>'quantity')::integer not between 1 and 20 or e->>'quantity' is null)) then raise exception 'Invalid quantity'; end if;
  select * into i from public.family_items where id=v.id and home_id=p_home for update;
  if i.id is null or not i.active or i.quantity<v.quantity then raise exception 'An item is no longer available. Refresh your menu.'; end if;
  update public.family_items set quantity=quantity-v.quantity where id=i.id;
  snapshot=snapshot||jsonb_build_array(jsonb_build_object('id',i.id,'name',i.name,'kind',i.kind,'quantity',v.quantity));
 end loop;
 insert into public.family_orders(home_id,user_id,display_name,items,client_id) values(p_home,auth.uid(),m.display_name,snapshot,p_client) returning id into o;
 return o;
end $$;
create or replace function public.family_order_status(p_id uuid,p_status text) returns void
language plpgsql security definer set search_path = '' as $$
declare o public.family_orders; v jsonb; r text;
begin
 select * into o from public.family_orders where id=p_id for update; r=public.family_role(o.home_id);
 if o.id is null or r is null then raise exception 'Request not found'; end if;
 if p_status=o.status then return; end if;
 if r not in ('owner','kitchen') and not (o.user_id=auth.uid() and p_status='cancelled' and o.status='pending') then raise exception 'Kitchen access required'; end if;
 if not ((o.status='pending' and p_status in ('ready','cancelled')) or (o.status='ready' and p_status in ('delivered','cancelled'))) then raise exception 'This request cannot change to that status'; end if;
 if p_status='cancelled' then
  for v in select value from jsonb_array_elements(o.items) order by value->>'id' loop
   update public.family_items set quantity=least(100000,quantity+(v->>'quantity')::integer) where id=(v->>'id')::uuid and home_id=o.home_id;
  end loop;
 end if;
 update public.family_orders set status=p_status,updated_at=now() where id=p_id;
end $$;
create or replace function public.family_message(p_home uuid,p_text text,p_client uuid) returns uuid
language plpgsql security definer set search_path = '' as $$
declare m public.family_members; i uuid;
begin
 select * into m from public.family_members where home_id=p_home and user_id=auth.uid() for update;
 if m.user_id is null then raise exception 'Join this household first'; end if;
 select id into i from public.family_messages where home_id=p_home and user_id=auth.uid() and client_id=p_client;
 if i is not null then return i; end if;
 if (select count(*) from public.family_messages where user_id=auth.uid() and created_at>now()-interval '1 minute')>=5 then raise exception 'Please wait before sending another message'; end if;
 insert into public.family_messages(home_id,user_id,display_name,text,client_id) values(p_home,auth.uid(),m.display_name,trim(p_text),p_client) returning id into i;
 return i;
end $$;
create or replace function public.family_tv_next(p_home uuid) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare m public.family_messages;
begin
 if coalesce(public.family_role(p_home),'') not in ('owner','kitchen') then raise exception 'Household owner or kitchen access required'; end if;
 select * into m from public.family_messages where home_id=p_home and announced_at is null and created_at>now()-interval '24 hours'
 and (claimed_at is null or claimed_at<now()-interval '2 minutes') order by created_at limit 1 for update skip locked;
 if m.id is null then return null; end if;
 update public.family_messages set claimed_at=now(),claimed_by=auth.uid() where id=m.id;
 return jsonb_build_object('id',m.id,'name',m.display_name,'text',m.text);
end $$;
create or replace function public.family_tv_ack(p_id uuid) returns void
language plpgsql security definer set search_path = '' as $$
begin
 update public.family_messages set announced_at=now() where id=p_id and claimed_by=auth.uid()
 and public.family_role(home_id) in ('owner','kitchen');
end $$;
create or replace function public.family_rotate(p_home uuid) returns void
language plpgsql security definer set search_path = '' as $$
begin
 if public.family_role(p_home) is distinct from 'owner' then raise exception 'Only the owner can change invitation codes'; end if;
 update public.family_homes set invite_code=replace(gen_random_uuid()::text,'-',''),kitchen_code=replace(gen_random_uuid()::text,'-','') where id=p_home;
end $$;
create or replace function public.family_remove(p_home uuid,p_user uuid) returns void
language plpgsql security definer set search_path = '' as $$
begin
 if public.family_role(p_home) is distinct from 'owner' then raise exception 'Only the owner can remove members'; end if;
 if p_user=auth.uid() then raise exception 'The owner cannot be removed'; end if;
 delete from public.family_members where home_id=p_home and user_id=p_user;
end $$;
-- Restrict every RPC in this feature, without touching unrelated Air OS functions.
do $$ declare f record; begin
 for f in select oid::regprocedure as signature from pg_proc where pronamespace='public'::regnamespace and proname like 'family_%' loop
  execute format('revoke all on function %s from public,anon,authenticated',f.signature);
  execute format('grant execute on function %s to authenticated',f.signature);
 end loop;
end $$;
notify pgrst,'reload schema';
commit;
