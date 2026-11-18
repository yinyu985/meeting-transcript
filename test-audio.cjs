// node test-audio.cjs — deterministic PCM and paragraph tests, no microphone required.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const source = fs.readFileSync(__dirname + '/app.js', 'utf8');
const ctx = vm.createContext({console, crypto: {randomUUID}, Date, Float32Array});
const extract = (start, end) => source.slice(source.indexOf(start), source.indexOf(end));
vm.runInContext(`
const FRAME_MS=20, RATE=16000;
const cfg={startFrames:3,hangoverMs:700,prerollMs:300,minMs:300,maxMs:20000,paragraphGapMs:1800,paragraphMaxMs:60000};
const state={channels:new Map(),lines:[]};
const el={pressure:{style:{}},empty:{style:{}},copy:{}};
const rms=f=>Math.sqrt(f.reduce((n,x)=>n+x*x,0)/f.length);
const packets=[];
const encodeWav=frames=>frames.reduce((n,f)=>n+f.length,0);
const send=(samples,at,dur,id,meta)=>packets.push({samples,at,dur,id,...meta});
function render(){} function syncClear(){} function syncLabels(){} function persist(){}
function clientLog(){} function toast(){} function attachChannel(){} function attachCaret(){} function scrollToLatest(){}
${extract('function createChannel(', 'async function attachChannel(')}
${extract('function watchSilence(', 'function onFrame(')}
${extract('function onFrame(', '// ---------- 发送 ----------')}
${extract('function push(', '// ---------- 滚动 ----------')}
function feed(ch,n,level=.1){for(let i=0;i<n;i++)onFrame(ch,new Float32Array(320).fill(level));}
`, ctx);
vm.runInContext(`
const ch=createChannel('mic',null);state.channels.set('mic',ch);
feed(ch,2255); flush(ch,ch.frames.length*20);
`, ctx);
const packets = vm.runInContext('packets', ctx);
assert.equal(packets.reduce((n,p)=>n+p.samples,0),2255*320,'45.1 seconds: every sample retained');
assert.equal(packets.length,3);
assert.equal(packets[0].group,packets[2].group);
assert.equal(packets[0].end,packets[1].at);
assert.equal(packets[1].end,packets[2].at);
vm.runInContext(`packets.length=0;const brief=createChannel('mic',null);feed(brief,5);flush(brief,brief.frames.length*20,'stop');`,ctx);
assert.equal(packets[0].samples,1600,'100 ms speech on explicit stop must not be discarded');
vm.runInContext(`
packets.length=0;
const tiny=createChannel('mic',null);feed(tiny,1005);flush(tiny,tiny.frames.length*20);
`,ctx);
assert.equal(packets.reduce((n,p)=>n+p.samples,0),1005*320,'100 ms tail after hard cut survives');
vm.runInContext(`
packets.length=0;
const long=createChannel('mic',null);feed(long,3500);flush(long,long.frames.length*20);
`,ctx);
assert.notEqual(packets[0].group,packets[3].group,'long speech has bounded paragraphs');
vm.runInContext(`
packets.length=0;
const a=createChannel('remote',null);
feed(a,50);feed(a,50,0);feed(a,50);feed(a,100,0);feed(a,50);flush(a,a.frames.length*20);
`,ctx);
assert.equal(packets[0].group,packets[1].group,'one second pause merges');
assert.notEqual(packets[1].group,packets[2].group,'two second pause separates');
vm.runInContext(extract('const SILENT_FRAMES_MS', 'function onFrame(') + `
var toasted=[];function toast(m){toasted.push(m)}
state.recording=true;
const quiet=createChannel('remote',null);
`,ctx);
vm.runInContext('feed(quiet,200,0.0005)',ctx);
assert.equal(vm.runInContext('quiet.silentWarned',ctx),false,'4 秒静音不提示');
vm.runInContext('feed(quiet,600,0.0005)',ctx);
assert.equal(vm.runInContext('quiet.silentWarned',ctx),true,'静音超过 15 秒提示');
assert.equal(vm.runInContext('toasted.length',ctx),1,'只提示一次');
vm.runInContext("const loud=createChannel('mic',null);feed(loud,900,0.2)",ctx);
assert.equal(vm.runInContext('loud.silentWarned',ctx),false,'有声音不提示');
console.log('PASS: silent-channel watchdog warns once only');
vm.runInContext(`
push('对方开头',100,{kind:'remote',group:'r'});
push('我插话',200,{kind:'mic',group:'m'});
push('对方继续',300,{kind:'remote',group:'r',end:400});
push('更早返回较晚',50,{kind:'mic',group:'early'});
`,ctx);
const lines=vm.runInContext('state.lines',ctx);
assert.equal(lines.length,3);
assert.equal(lines[1].text,'对方开头对方继续');
assert.equal(lines[2].text,'我插话');
assert.equal(lines[0].t,50);
vm.runInContext(extract('function observePerformance(', '// ---------- 发送 ----------'),ctx);
vm.runInContext(`
state.samples=[];state.pendingMs=0;state.segmentMs=20000;
observePerformance(20000,9000);
observePerformance(20000,1000);
observePerformance(20000,1000);
`,ctx);
assert.equal(vm.runInContext('state.segmentMs',ctx),20000,'do not tune from too few observations');
vm.runInContext('observePerformance(20000,1000)',ctx);
assert.equal(vm.runInContext('state.segmentMs',ctx),16000,'gradual reduction; cold first request excluded');
vm.runInContext('state.pendingMs=40000;observePerformance(20000,1000)',ctx);
assert.equal(vm.runInContext('state.segmentMs',ctx),20000,'backlog restores conservative cap');
console.log('PASS: continuous PCM, short tail, pause grouping, overlapping speakers, chronological ordering, performance adaptation');

// Stop must deliver the partial worklet frame before acknowledging completion.
{
  const messages=[];let Recorder;
  const audio=vm.createContext({
    sampleRate:16000, Float32Array,
    AudioWorkletProcessor:class {constructor(){this.port={postMessage:m=>messages.push(m)};}},
    registerProcessor:(name,ctor)=>Recorder=ctor,
  });
  vm.runInContext(fs.readFileSync(__dirname+'/recorder-worklet.js','utf8'),audio);
  const recorder=new Recorder();
  for(let i=0;i<3;i++)recorder.process([[new Float32Array(128).fill(.1)]]);
  recorder.port.onmessage({data:{type:'drain'}});
  assert.equal(messages[0].length,320);
  assert.equal(messages[1].length,64);
  assert.equal(messages[2].type,'drained');
  recorder.process([[new Float32Array(128)]]);
  assert.equal(messages.length,3,'drained worklet produces no new frames');
  console.log('PASS: stop drains partial frame before acknowledgement');
}

(async () => {
  let calls=0, active=0, peak=0;
  const q=vm.createContext({
    console:{info(){},error(){}}, crypto:{randomUUID}, performance,
    AbortController,setTimeout,clearTimeout,queueMicrotask,
    fetch:async()=>{
      active++;peak=Math.max(peak,active);calls++;
      await new Promise(r=>setTimeout(r,2));active--;
      if(calls===1)throw Error('simulated backend failure');
      return {ok:true,json:async()=>({text:'测试'})};
    },
  });
  vm.runInContext(`
    const state={chain:Promise.resolve(),generation:0,pending:0,pendingMs:0,lang:'',recording:false};
    const results=[];function push(...args){results.push(args);}
    function updateProcessingStatus(){} function observePerformance(){}
    function toast(){} function clockOf(){return '00:00';}
    let stops=0;async function stop(){stops++;state.recording=false;}
    ${extract('function send(', '// ---------- 生命周期 ----------')}
    send('wav',100,20000,'mic');send('wav',200,20000,'remote');
  `,q);
  await vm.runInContext('state.chain',q);
  assert.equal(peak,1,'client schedules one request at a time');
  assert.equal(vm.runInContext('results.length',q),1,'failure does not block next request');
  assert.equal(vm.runInContext('state.pendingMs',q),0);
  assert.equal(vm.runInContext('state.pending',q),0);
  vm.runInContext(`send('wav',300,1000,'mic');state.generation++;`,q);
  await vm.runInContext('state.chain',q);
  assert.equal(calls,2,'cleared queued job is skipped');
  assert.equal(vm.runInContext('state.pendingMs',q),0);
  vm.runInContext(`state.recording=true;send('wav',400,120000,'mic');`,q);
  await vm.runInContext('state.chain',q);
  assert.equal(vm.runInContext('stops',q),1,'overload stops new capture');
  assert.equal(vm.runInContext('state.pendingMs',q),0,'already captured audio still processed');
  console.log('PASS: queue accounting, failure recovery, stale-job cancellation, overload protection');
})().catch(err=>{console.error(err);process.exitCode=1;});
