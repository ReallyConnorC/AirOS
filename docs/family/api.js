(function(root){
 'use strict';
 class AirClient {
  constructor(config,storage,transport){this.config=config;this.storage=storage;this.fetch=transport||fetch;this.refreshing=null;try{this.session=JSON.parse(storage.getItem('air-family-session')||'null')}catch{this.session=null}}
  save(session){this.session=session;if(session)this.storage.setItem('air-family-session',JSON.stringify(session));else this.storage.removeItem('air-family-session')}
  async raw(path,body,token,method){const res=await this.fetch(this.config.url+path,{method:method||(body===undefined?'GET':'POST'),headers:{apikey:this.config.key,'Content-Type':'application/json',Authorization:'Bearer '+(token||this.config.key)},body:body===undefined?undefined:JSON.stringify(body)});let data;try{data=await res.json()}catch{data=null}if(!res.ok){const e=new Error(data?.message||data?.error_description||data?.msg||'Connection failed ('+res.status+')');e.status=res.status;throw e}return data}
  async signin(email,password){const s=await this.raw('/auth/v1/token?grant_type=password',{email,password});this.save({...s,expires_at:Date.now()+s.expires_in*1000});return s}
  async signup(email,password,name){const s=await this.raw('/auth/v1/signup',{email,password,data:{display_name:name}});if(s.access_token)this.save({...s,expires_at:Date.now()+s.expires_in*1000});return s}
  async token(){if(!this.session)throw new Error('Sign in first');if(this.session.expires_at>Date.now()+60000)return this.session.access_token;if(!this.refreshing)this.refreshing=this.raw('/auth/v1/token?grant_type=refresh_token',{refresh_token:this.session.refresh_token}).then(s=>{this.save({...s,expires_at:Date.now()+s.expires_in*1000});return s.access_token}).catch(e=>{if(e.status===400||e.status===401)this.save(null);throw e}).finally(()=>{this.refreshing=null});return this.refreshing}
  async request(path,body){let token=await this.token();try{return await this.raw(path,body,token)}catch(e){if(e.status!==401)throw e;this.session.expires_at=0;token=await this.token();return this.raw(path,body,token)}}
  rpc(name,body={}){return this.request('/rest/v1/rpc/family_'+name,body)}
  rows(table,home,query=''){return this.request('/rest/v1/family_'+table+'?home_id=eq.'+encodeURIComponent(home)+'&'+query)}
  async logout(){try{await this.request('/auth/v1/logout',{})}finally{this.save(null)}}
 }
 root.AirClient=AirClient;if(typeof module!=='undefined')module.exports=AirClient;
})(globalThis);
