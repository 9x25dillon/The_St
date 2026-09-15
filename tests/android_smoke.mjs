// On-device smoke test. Forward port 9224 to The Saint's debug WebView first.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const parityCases = JSON.parse(await readFile(new URL('./parity_cases.json', import.meta.url), 'utf8'));
let ws;
try {
  let tabs;
  for(let i=0;i<60;i++){
    try{tabs=await (await fetch('http://127.0.0.1:9224/json')).json();break;}catch{await new Promise(r=>setTimeout(r,100));}
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

  await until("typeof token === 'string'");
  assert.equal(await evaluate("typeof window.SaintAndroid.request"),'function');
  // auto, notes, tiktok, youtube, instagram, x, spotify, reddit, amazon, usage --
  // firefox/chrome stay desktop-only (they read a live local browser profile, which
  // Android's sandboxed picker model can't do).
  assert.equal(await evaluate("document.querySelectorAll('#source option').length"),10);
  await evaluate("document.getElementById('demo').click()");
  await until("document.getElementById('count').textContent === '60 passages'");
  assert.equal(await evaluate("document.querySelectorAll('.passage').length"),40);
  await evaluate("document.getElementById('search').value='piano';document.getElementById('search').dispatchEvent(new Event('input'))");
  assert.equal(await evaluate("document.querySelectorAll('.passage').length"),12);
  await evaluate("document.getElementById('clear').click()");
  await until("document.getElementById('count').textContent === '0 passages'");
  // Source stays on its default "auto" selection -- exercises auto-detect end to end
  // through a real File object, not just the synthetic {name,text} dicts the bridge
  // checks below use.
  await evaluate(`(async()=>{const input=document.getElementById('files');const transfer=new DataTransfer();transfer.items.add(new File(['# Today\\n\\nA quiet walk. <img src=x onerror=alert(1)>'],'journal.md',{type:'text/plain'}));input.files=transfer.files;document.getElementById('import').click();})()`);
  await until("document.getElementById('count').textContent === '1 passages'");
  assert.equal(await evaluate("document.querySelectorAll('.passage img').length"),0);
  assert.match(await evaluate("document.querySelector('.passage').textContent"),/quiet walk/);
  assert.match(await evaluate("document.getElementById('status').textContent"),/Detected personal notes/);
  await evaluate("document.getElementById('demo').click()");
  await until("document.getElementById('count').textContent === '61 passages'");
  assert.match(await evaluate("document.getElementById('sources-row').textContent"),/personal notes.*1|sample journal.*60/);
  const chip=value=>`[...document.querySelectorAll('#filters button')].find(b=>b.dataset.filter===${JSON.stringify(value)})`;
  await evaluate(`${chip('origin:notes')}.click()`);
  assert.equal(await evaluate("document.querySelectorAll('.passage').length"),1);
  assert.equal(await evaluate("document.getElementById('showing').textContent"),'Showing 1 of 61 passages');
  await evaluate(`${chip('origin:notes')}.click()`);
  await evaluate("document.getElementById('demo').click()");
  await until("/already in your session/.test(document.getElementById('status').textContent)");
  assert.equal(await evaluate("document.getElementById('count').textContent"),'61 passages');

  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true);

  // Bridge-level checks bypass the file picker: feed {source, files} straight to
  // SaintAndroid.request, each starting from a clean slate.
  const bridge = async (path, body) => evaluate(`JSON.parse(SaintAndroid.request(${JSON.stringify(path)}, ${JSON.stringify(JSON.stringify(body))}))`);
  const nativeState = () => bridge('/api/state', {});

  // Shared parsing cases: the same file tests/test_parity.py runs against the desktop adapters.
  for (const c of parityCases) {
    await bridge('/api/clear', {});
    const res = await bridge('/api/import', {source:'auto', files:c.files});
    if (c.expect_error) { assert.match(res.error ?? '', new RegExp(c.expect_error), c.name); continue; }
    assert.equal(res.error, undefined, `${c.name}: ${res.error}`);
    assert.equal(res.detected_source, c.expect.detected_source, c.name);
    const st = await nativeState();
    assert.deepEqual(st.records.map(r=>[r.text, r.source]), c.expect.records, c.name);
    if (c.expect.when) assert.deepEqual(st.records.map(r=>r.when===null?null:Math.floor(r.when)), c.expect.when, c.name);
    assert.deepEqual(st.categories, c.expect.categories ?? [], c.name);
    assert.equal(st.watches, c.expect.watches ?? 0, c.name);
    assert.equal(st.watch_times, undefined, 'watch timestamps stay private');
  }

  // Failed import keeps the session; re-importing the same export is a no-op.
  await bridge('/api/clear', {});
  const tiktok = parityCases[0].files;
  let res = await bridge('/api/import', {source:'tiktok', files:tiktok});
  assert.deepEqual([res.changed, res.added, res.skipped], [true, 3, 0]);
  assert.ok((await bridge('/api/import', {source:'notes', files:[{name:'bad.pdf', text:'wrong type'}]})).error);
  res = await bridge('/api/import', {source:'tiktok', files:tiktok});
  assert.deepEqual([res.changed, res.added, res.skipped], [false, 0, 3]);
  let st = await nativeState();
  assert.deepEqual([st.records.length, st.watches, st.records[0].origin], [3, 2, 'tiktok']);

  // One file per request still checks the whole selection for mixed sources.
  res = await bridge('/api/import', {source:'auto', names:['diary.md','posts.csv'], files:[{name:'diary.md', text:'Gardens.'}]});
  assert.match(res.error ?? '', /different sources/);

  // Past 5,000 passages a source keeps its newest entries, whatever order its files arrive in.
  await bridge('/api/clear', {});
  const day = i => new Date(Date.UTC(2020, 0, 1) + i * 86400e3).toISOString().slice(0, 10);
  const usage = (from, to) => ({source:'usage', files:[{name:'usage.json', text:JSON.stringify(
    Array.from({length:to-from}, (_, k) => ({app:'Notes', minutes:1, date:day(from+k)})))}]});
  res = await bridge('/api/import', usage(3000, 5200));
  assert.deepEqual([res.added, res.dropped], [2200, 0]);
  res = await bridge('/api/import', usage(0, 3000));
  assert.deepEqual([res.added, res.dropped], [2800, 200]);
  st = await nativeState();
  assert.equal(st.records.length, 5000);
  assert.equal(Math.min(...st.records.map(r=>r.when)), Date.UTC(2020, 0, 1) / 1000 + 200 * 86400);

  await evaluate("document.getElementById('clear').click()");
  await until("document.getElementById('count').textContent === '0 passages'");
  assert.equal(await evaluate("document.getElementById('empty').hidden"),false);
  console.log(`Android smoke passed: native bridge, mobile sources, sample, search, note import, auto-detect, source filter, duplicate imports, ${parityCases.length} shared parsing cases, newest-first trimming, failed-import recovery, escaping, layout, clear.`);
} finally { ws?.close(); }
