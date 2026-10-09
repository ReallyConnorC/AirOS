-- Air Family 1.1: per-device sender names with shared account sign-in.
begin;
alter table public.family_orders add column if not exists device_id uuid;
alter table public.family_messages add column if not exists device_id uuid;

create or replace function public.family_order_device(p_home uuid,p_items jsonb,p_client uuid,p_display_name text,p_device uuid) returns uuid
language plpgsql security definer set search_path = '' as $$
declare m public.family_members; v record; i public.family_items; snapshot jsonb='[]'; o uuid;
begin
 if p_device is null or p_display_name is null or length(trim(p_display_name)) not between 1 and 32 or p_display_name ~ '[[:cntrl:]]' then raise exception 'Enter a device name between 1 and 32 characters'; end if;
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
 insert into public.family_orders(home_id,user_id,display_name,items,client_id,device_id) values(p_home,auth.uid(),trim(p_display_name),snapshot,p_client,p_device) returning id into o;
 return o;
end $$;

create or replace function public.family_message_device(p_home uuid,p_text text,p_client uuid,p_display_name text,p_device uuid) returns uuid
language plpgsql security definer set search_path = '' as $$
declare m public.family_members; i uuid;
begin
 if p_device is null or p_display_name is null or length(trim(p_display_name)) not between 1 and 32 or p_display_name ~ '[[:cntrl:]]' then raise exception 'Enter a device name between 1 and 32 characters'; end if;
 select * into m from public.family_members where home_id=p_home and user_id=auth.uid() for update;
 if m.user_id is null then raise exception 'Join this household first'; end if;
 select id into i from public.family_messages where home_id=p_home and user_id=auth.uid() and client_id=p_client;
 if i is not null then return i; end if;
 if (select count(*) from public.family_messages where user_id=auth.uid() and created_at>now()-interval '1 minute')>=5 then raise exception 'Please wait before sending another message'; end if;
 insert into public.family_messages(home_id,user_id,display_name,text,client_id,device_id) values(p_home,auth.uid(),trim(p_display_name),trim(p_text),p_client,p_device) returning id into i;
 return i;
end $$;

revoke all on function public.family_order_device(uuid,jsonb,uuid,text,uuid),public.family_message_device(uuid,text,uuid,text,uuid) from public,anon;
grant execute on function public.family_order_device(uuid,jsonb,uuid,text,uuid),public.family_message_device(uuid,text,uuid,text,uuid) to authenticated;
notify pgrst,'reload schema';
commit;
