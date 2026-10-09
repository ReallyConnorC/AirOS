(function(root){
 'use strict';
 class DeviceProfile {
  constructor(storage,uuid){this.storage=storage;this.id=storage.getItem('air-family-device');if(!this.id){this.id=(uuid||(()=>root.crypto.randomUUID()))();storage.setItem('air-family-device',this.id)}}
  get name(){return this.storage.getItem('air-family-name')||''}
  setName(value){const name=String(value).trim();if(!name||name.length>32||/[\x00-\x1f\x7f]/.test(name))throw new Error('Enter a name between 1 and 32 characters');this.storage.setItem('air-family-name',name);return name}
  sender(){if(!this.name)throw new Error('Choose your name on this device first');return {p_device:this.id,p_display_name:this.name}}
  owns(row,user){return row.user_id===user&&(row.device_id===this.id||(!row.device_id&&row.display_name===this.name))}
 }
 root.DeviceProfile=DeviceProfile;if(typeof module!=='undefined')module.exports=DeviceProfile;
})(globalThis);
