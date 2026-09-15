// Optional real-browser smoke test. Start python run.py first.
// Run: node tests/browser_smoke.mjs (requires Chromium on PATH).
import {spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
const profile=await mkdtemp(join(tmpdir(),'saint-ui-'));
const chrome=spawn('chromium',['--headless','--no-sandbox','--disable-gpu','--remote-debugging-port=9223',`--user-data-dir=${profile}`,'about:blank'],{stdio:'ignore'});
let ws;
try {
  let tabs;
  for(let i=0;i<60;i++){
    try{tabs=await (await fetch('http://127.0.0.1:9223/json')).json();break;}catch{await new Promise(r=>setTimeout(r,100));}
  }
  if(!tabs)throw Error('Chromium did not start');
  ws=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  let id=0;const pending=new Map();
  ws.onmessage=e=>{const message=JSON.parse(e.data);if(pending.has(message.id)){const [resolve,reject]=pending.get(message.id);pending.delete(message.id);message.error?reject(Error(message.error.message)):resolve(message.result);}};
  const call=(method,params={})=>new Promise((resolve,reject)=>{const key=++id;pending.set(key,[resolve,reject]);ws.send(JSON.stringify({id:key,method,params}));});
  const evaluate=async expression=>{const r=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;};
  async function until(expression){for(let i=0;i<100;i++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,100));}throw Error(`Timed out: ${expression}; page: ${await evaluate("document.body.innerText")}`);}
  await call('Page.enable');
  await call('Runtime.enable');
  console.log('Navigation:',await call('Page.navigate',{url:process.env.SAINT_URL || 'http://127.0.0.1:8765'}));
  await until("typeof token === 'string'");
  await evaluate("document.getElementById('demo').click()");
  await until("document.getElementById('count').textContent === '60 passages'");
  assert.equal(await evaluate("document.querySelectorAll('.passage').length"),40);
  await evaluate("document.getElementById('search').value='piano';document.getElementById('search').dispatchEvent(new Event('input'))");
  assert.equal(await evaluate("document.querySelectorAll('.passage').length"),12);
  await evaluate("document.getElementById('clear').click()");
  await until("document.getElementById('count').textContent === '0 passages'");
  // Source stays on its default "auto" selection -- this also exercises auto-detect end to
  // end through a real File object, not just the synthetic {name,text} dicts unit tests use.
  await evaluate(`(async()=>{const input=document.getElementById('files');const transfer=new DataTransfer();transfer.items.add(new File(['# Today\\n\\nA quiet walk. <img src=x onerror=alert(1)>'],'journal.md',{type:'text/plain'}));input.files=transfer.files;document.getElementById('import').click();})()`);
  await until("document.getElementById('count').textContent === '1 passages'");
  assert.equal(await evaluate("document.querySelectorAll('.passage img').length"),0);
  assert.match(await evaluate("document.querySelector('.passage').textContent"),/quiet walk/);
  assert.match(await evaluate("document.getElementById('status').textContent"),/Detected personal notes/);
  const chip=value=>`[...document.querySelectorAll('#filters button')].find(b=>b.dataset.filter===${JSON.stringify(value)})`;
  // Island cards render from the map payload; injected here so this check doesn't need the model.
  await evaluate(`state.map={points:[{x:0,y:0,label:0,outlier:0.1,iws:0.18,signal:false,factors:{fit:0.9,clarity:0.02,recency:1,typical:0.2}}],stars:[],
    health:{snr:0,homogenization:1,profile_health:0,threshold:0.2,floor:0.25,held_back_by:[['clarity',1]]},activity_axis:{first_month:24288,bucket_months:1,buckets:3},
    islands:[{label:0,size:1,share:1,terms:['quiet','walk'],sources:[['notes',1]],kinds:[['note',1]],signal_share:0,first_when:1704067200,last_when:1704067200,
      activity:[1,0,0],trend:{label:'growing',island_recent:1,session_recent:0.25,since:1706745600},central:[0],labels:['Hiking']}],unclustered:2};render()`);
  assert.equal(await evaluate("document.querySelector('.island h4').textContent"),'quiet · walk');
  assert.match(await evaluate("document.querySelector('.island').textContent"),/From personal notes 1 — notes 1Closest assigned labels: Hiking/);
  assert.equal(await evaluate("document.getElementById('unclustered').textContent"),"2 passages didn't settle into any island.");
  assert.equal(await evaluate("document.querySelectorAll('.island img').length"),0);
  assert.equal(await evaluate("document.querySelector('.central small').textContent"),'journal.md · Today');
  assert.match(await evaluate("document.querySelector('.island .mix-legend').textContent"),/100% written, searched, or bought by you/);
  assert.equal(await evaluate("document.querySelectorAll('.island .spark rect').length"),1);
  assert.match(await evaluate("document.querySelector('.island').textContent"),/Busiest in Jan 2024\. Growing lately: 100% of its dated passages are from Feb 2024 on, against 25% of the whole session\./);
  assert.match(await evaluate("document.getElementById('health-note').textContent"),/0% of passages score as signal \(0\.2 or higher\)\. Of the rest, 100% are held back most by sitting between two islands/);
  assert.equal(await evaluate("document.querySelector('.passage .why summary').textContent"),'Why flagged as noise?');
  assert.match(await evaluate("document.querySelector('.passage .why').textContent"),/Score 0\.18; signal needs 0\.2\. Each factor counts as at least 0\.25.*Clearly in one island, not between two: 0\.02, counts as 0\.27 \(lowest\)/);
  assert.match(await evaluate("document.getElementById('mix').textContent"),/100% written, searched, or bought by you/);
  await evaluate("document.querySelector('.island button').click()");
  assert.equal(await evaluate("document.querySelector('.island').classList.contains('active')"),true);
  assert.equal(await evaluate(`${chip('island:0')}.getAttribute('aria-pressed')`),'true');
  await evaluate("state.map=null;render()");
  assert.equal(await evaluate("document.getElementById('islands-wrap').hidden"),true);
  await evaluate("document.getElementById('demo').click()");
  await until("document.getElementById('count').textContent === '61 passages'");
  assert.match(await evaluate("document.getElementById('sources-row').textContent"),/personal notes.*1|sample journal.*60/);
  // Source filter: faceted chip counts ignore their own group, so the other chip keeps its total.
  await evaluate(`${chip('origin:notes')}.click()`);
  assert.equal(await evaluate("document.querySelectorAll('.passage').length"),1);
  assert.equal(await evaluate("document.getElementById('showing').textContent"),'Showing 1 of 61 passages');
  assert.equal(await evaluate(`${chip('origin:notes')}.getAttribute('aria-pressed')`),'true');
  assert.match(await evaluate(`${chip('origin:demo')}.textContent`),/sample journal · 60/);
  await evaluate("document.getElementById('search').value='walk';document.getElementById('search').dispatchEvent(new Event('input'))");
  assert.match(await evaluate(`${chip('origin:demo')}.textContent`),/sample journal · 12/);
  await evaluate("[...document.querySelectorAll('#filters button')].find(b=>b.textContent==='Clear filters').click()");
  assert.equal(await evaluate("document.querySelectorAll('.passage').length"),13);
  // Re-importing the same source is a no-op, not a doubled session.
  await evaluate("document.getElementById('demo').click()");
  await until("/already in your session/.test(document.getElementById('status').textContent)");
  assert.equal(await evaluate("document.getElementById('count').textContent"),'61 passages');
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true);
  await evaluate("document.getElementById('clear').click()");
  await until("document.getElementById('count').textContent === '0 passages'");
  assert.equal(await evaluate("document.getElementById('empty').hidden"),false);
  console.log('Browser smoke passed: sample, search, note upload, text escaping, island cards, score explanations, session mix, source filter, duplicate import, mobile layout, clear.');
} finally {
  ws?.close();chrome.kill();
  await new Promise(resolve=>chrome.exitCode!==null?resolve():chrome.once('exit',resolve));
  await rm(profile,{recursive:true,force:true});
}
