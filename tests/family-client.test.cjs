const {test}=require('node:test');
const assert=require('node:assert/strict');
const AirClient=require('../docs/family/api.js');
function storage(){const map=new Map();return {getItem:k=>map.get(k),setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)}}
function response(data,status=200){return {ok:status<400,status,json:async()=>data}}
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
