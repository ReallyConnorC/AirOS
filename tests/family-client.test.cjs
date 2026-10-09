const {test}=require('node:test');
const assert=require('node:assert/strict');
const AirClient=require('../docs/family/api.js');
const DeviceProfile=require('../docs/family/profile.js');
function storage(){const map=new Map();return {getItem:k=>map.get(k),setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)}}
function response(data,status=200){return {ok:status<400,status,json:async()=>data}}
test('two devices on the same account keep independent sender names and request history',()=>{
 const aStore=storage(),bStore=storage();
 const a=new DeviceProfile(aStore,()=> 'device-a'),b=new DeviceProfile(bStore,()=> 'device-b');
 a.setName('Alex');b.setName('Sam');
 assert.deepEqual(a.sender(),{p_device:'device-a',p_display_name:'Alex'});
 assert.deepEqual(b.sender(),{p_device:'device-b',p_display_name:'Sam'});
 a.setName('Alice');assert.equal(b.name,'Sam');
 const reload=new DeviceProfile(aStore,()=> 'should-not-change');assert.equal(reload.id,'device-a');assert.equal(reload.name,'Alice');
 assert.equal(a.owns({user_id:'shared',device_id:'device-a'},'shared'),true);
 assert.equal(a.owns({user_id:'shared',device_id:'device-b'},'shared'),false);
 assert.equal(a.owns({user_id:'other',device_id:'device-a'},'shared'),false);
 assert.throws(()=>a.setName('  '),/Enter a name/);assert.throws(()=>a.setName('A\nB'),/Enter a name/);
});
test('concurrent requests refresh an expired session only once',async()=>{
 let refreshes=0;const client=new AirClient({url:'https://example.com',key:'public'},storage(),async(url,opts)=>{
  if(url.includes('grant_type=refresh_token')){refreshes++;await new Promise(resolve=>setTimeout(resolve,5));return response({access_token:'new',refresh_token:'rotated',expires_in:3600,user:{id:'user'}})}
  assert.equal(opts.headers.Authorization,'Bearer new');return response([]);
 });client.save({access_token:'old',refresh_token:'old-refresh',expires_at:0});await Promise.all([client.rpc('me'),client.rpc('me')]);assert.equal(refreshes,1);assert.equal(client.session.refresh_token,'rotated');
});
test('network failure retains the session for reconnecting',async()=>{
 const client=new AirClient({url:'https://example.com',key:'public'},storage(),async()=>{throw new Error('offline')});client.save({access_token:'old',refresh_token:'refresh',expires_at:0});await assert.rejects(client.token(),/offline/);assert.ok(client.session);
});
test('invalid refresh token clears the session',async()=>{
 const client=new AirClient({url:'https://example.com',key:'public'},storage(),async()=>response({message:'invalid refresh'},400));client.save({access_token:'old',refresh_token:'refresh',expires_at:0});await assert.rejects(client.token(),/invalid refresh/);assert.equal(client.session,null);
});

test('browser fetch retains its required global receiver',async()=>{
 const original=globalThis.fetch;
 globalThis.fetch=async function(url,opts){
  assert.equal(this,globalThis,'fetch must use the browser global receiver');
  assert.equal(url,'https://example.com/auth/v1/token?grant_type=password');
  assert.equal(JSON.parse(opts.body).email,'person@example.com');
  return response({access_token:'token',refresh_token:'refresh',expires_in:3600});
 };
 try{
  const client=new AirClient({url:'https://example.com',key:'public'},storage());
  await client.signin('person@example.com','test-password');
  assert.equal(client.session.access_token,'token');
 }finally{globalThis.fetch=original}
});
