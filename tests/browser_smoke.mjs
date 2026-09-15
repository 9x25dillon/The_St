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
  await evaluate("document.getElementById('demo').click()");
  await until("document.getElementById('count').textContent === '61 passages'");
  assert.match(await evaluate("document.getElementById('sources-row').textContent"),/personal notes.*1|sample journal.*60/);
  // Source filter: faceted chip counts ignore their own group, so the other chip keeps its total.
  const chip=value=>`[...document.querySelectorAll('#filters button')].find(b=>b.dataset.filter===${JSON.stringify(value)})`;
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
  console.log('Browser smoke passed: sample, search, note upload, text escaping, source filter, duplicate import, mobile layout, clear.');
} finally {
  ws?.close();chrome.kill();
  await new Promise(resolve=>chrome.exitCode!==null?resolve():chrome.once('exit',resolve));
  await rm(profile,{recursive:true,force:true});
}
