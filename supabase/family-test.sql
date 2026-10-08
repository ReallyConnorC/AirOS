-- Real database integration tests. All users, stock and requests are rolled back.
begin;
select set_config('air.test_owner',gen_random_uuid()::text,true),set_config('air.test_member',gen_random_uuid()::text,true),set_config('air.test_stranger',gen_random_uuid()::text,true);
insert into auth.users(id,email) values
 (current_setting('air.test_owner')::uuid,current_setting('air.test_owner')||'@air-test.invalid'),
 (current_setting('air.test_member')::uuid,current_setting('air.test_member')||'@air-test.invalid'),
 (current_setting('air.test_stranger')::uuid,current_setting('air.test_stranger')||'@air-test.invalid');
create function pg_temp.assert_true(ok boolean,label text) returns void language plpgsql as $$ begin if ok is distinct from true then raise exception 'FAILED: %',label;end if;end $$;
set local role authenticated;
select set_config('request.jwt.claim.sub',current_setting('air.test_owner'),true);
select set_config('air.test_home',public.family_create('Integration test','Owner')::text,true);
select set_config('air.test_code',public.family_me()->0->>'invite_code',true);
select set_config('air.test_food',public.family_item_save(current_setting('air.test_home')::uuid,null,'Sandwich','food',3)::text,true);
select set_config('air.test_drink',public.family_item_save(current_setting('air.test_home')::uuid,null,'Juice','drink',2)::text,true);
select set_config('request.jwt.claim.sub',current_setting('air.test_member'),true);
select public.family_join(current_setting('air.test_code'),'Alex');
select pg_temp.assert_true(public.family_me()->0->>'role'='member','member role');
select pg_temp.assert_true(public.family_me()->0->>'invite_code' is null,'member cannot read invitations');
do $$ begin
 begin perform public.family_item_save(current_setting('air.test_home')::uuid,null,'Forbidden','food',3);raise exception 'Expected kitchen permission error';
 exception when others then if sqlerrm<>'Kitchen access required' then raise;end if;end;
end $$;
select set_config('air.test_nonce',gen_random_uuid()::text,true);
select set_config('air.test_order',public.family_order(current_setting('air.test_home')::uuid,
 jsonb_build_array(jsonb_build_object('id',current_setting('air.test_food'),'quantity',2),jsonb_build_object('id',current_setting('air.test_drink'),'quantity',1)),current_setting('air.test_nonce')::uuid)::text,true);
select pg_temp.assert_true((select quantity=1 from public.family_items where id=current_setting('air.test_food')::uuid),'food reserved');
select pg_temp.assert_true((select quantity=1 from public.family_items where id=current_setting('air.test_drink')::uuid),'drink reserved');
select pg_temp.assert_true(public.family_order(current_setting('air.test_home')::uuid,
 jsonb_build_array(jsonb_build_object('id',current_setting('air.test_food'),'quantity',2)),current_setting('air.test_nonce')::uuid)=current_setting('air.test_order')::uuid,'duplicate request is idempotent');
select pg_temp.assert_true((select quantity=1 from public.family_items where id=current_setting('air.test_food')::uuid),'duplicate does not reserve again');
do $$ begin
 begin perform public.family_order(current_setting('air.test_home')::uuid,jsonb_build_array(jsonb_build_object('id',current_setting('air.test_food'),'quantity',2)),gen_random_uuid());raise exception 'Expected unavailable item error';
 exception when others then if sqlerrm<>'An item is no longer available. Refresh your menu.' then raise;end if;end;
 begin perform public.family_order(current_setting('air.test_home')::uuid,null,gen_random_uuid());raise exception 'Expected empty request error';
 exception when others then if sqlerrm<>'Choose 1 to 10 items' then raise;end if;end;
 begin perform public.family_tv_next(current_setting('air.test_home')::uuid);raise exception 'Expected TV permission error';
 exception when others then if sqlerrm<>'Household owner or kitchen access required' then raise;end if;end;
end $$;
select public.family_order_status(current_setting('air.test_order')::uuid,'cancelled');
select public.family_order_status(current_setting('air.test_order')::uuid,'cancelled');
select pg_temp.assert_true((select quantity=3 from public.family_items where id=current_setting('air.test_food')::uuid),'cancel returns stock exactly once');
select pg_temp.assert_true((select quantity=2 from public.family_items where id=current_setting('air.test_drink')::uuid),'drink returned');
select set_config('air.test_message',public.family_message(current_setting('air.test_home')::uuid,'Hello TV',gen_random_uuid())::text,true);
select set_config('request.jwt.claim.sub',current_setting('air.test_stranger'),true);
select pg_temp.assert_true((select count(*)=0 from public.family_items where home_id=current_setting('air.test_home')::uuid),'stranger cannot read stock');
select pg_temp.assert_true((select count(*)=0 from public.family_orders where home_id=current_setting('air.test_home')::uuid),'stranger cannot read requests');
select pg_temp.assert_true((select count(*)=0 from public.family_messages where home_id=current_setting('air.test_home')::uuid),'stranger cannot read messages');
select set_config('request.jwt.claim.sub',current_setting('air.test_owner'),true);
select pg_temp.assert_true(public.family_tv_next(current_setting('air.test_home')::uuid)->>'name'='Alex','TV receives member display name');
select pg_temp.assert_true(public.family_tv_next(current_setting('air.test_home')::uuid) is null,'claimed message not delivered twice');
select public.family_tv_ack(current_setting('air.test_message')::uuid);
select pg_temp.assert_true((select announced_at is not null from public.family_messages where id=current_setting('air.test_message')::uuid),'TV acknowledgement recorded');
select pg_temp.assert_true(not has_function_privilege('anon','public.family_message(uuid,text,uuid)','execute'),'anonymous messages refused');
select 'PASS: stock, idempotency, cancellation, membership isolation and TV delivery' as result;
rollback;
