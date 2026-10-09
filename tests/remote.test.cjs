const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const html=fs.readFileSync('airos/ui/index.html','utf8');
function remote(){
 const handlers={},state={opened:0,menus:0};let timer;
 const cell={go:()=>state.opened++,app:{n:'YouTube'}};
 const env={KEYS:{Enter:'OK',ArrowRight:'Right'},ctx:null,pos:{r:0,c:0},active:()=>[[cell]],run:fn=>fn?.(),appMenu:()=>state.menus++,setTimeout:fn=>(timer=fn,1),clearTimeout:()=>timer=null,window:{addEventListener(){}},document:{addEventListener:(type,fn)=>(handlers[type]||=[]).push(fn)},onKey:e=>{if(e.key==='Enter')env.beginOK()}};
 vm.createContext(env);vm.runInContext(html.slice(html.indexOf('let okHold=null;'),html.indexOf('// Air-mouse remotes')),env);
 const event=key=>({key,preventDefault(){}});
 return {state,down:()=>handlers.keydown.forEach(fn=>fn(event('Enter'))),up:()=>handlers.keyup.forEach(fn=>fn(event('Enter'))),hold:()=>timer(),blur:()=>env.cancelOK()};
}
test('short OK opens once on release',()=>{const r=remote();r.down();r.down();assert.equal(r.state.opened,0);r.up();assert.equal(r.state.opened,1)});
test('held OK opens menu; repeats and release never launch',()=>{const r=remote();r.down();r.hold();r.down();r.up();assert.equal(r.state.menus,1);assert.equal(r.state.opened,0)});
test('focus loss cancels a pending OK',()=>{const r=remote();r.down();r.blur();r.up();assert.equal(r.state.opened,0)});
