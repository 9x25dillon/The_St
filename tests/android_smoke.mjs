// On-device smoke test. Forward port 9224 to The Saint's debug WebView first.
import assert from 'node:assert/strict';
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
  assert.equal(await evaluate("document.querySelectorAll('#source option').length"),2);
  await evaluate("document.getElementById('demo').click()");
  await until("document.getElementById('count').textContent === '60 passages'");
  assert.equal(await evaluate("document.querySelectorAll('.passage').length"),40);
  await evaluate("document.getElementById('search').value='piano';document.getElementById('search').dispatchEvent(new Event('input'))");
  assert.equal(await evaluate("document.querySelectorAll('.passage').length"),12);
  await evaluate(`(async()=>{const input=document.getElementById('files');const transfer=new DataTransfer();transfer.items.add(new File(['# Today\\n\\nA quiet walk. <img src=x onerror=alert(1)>'],'journal.md',{type:'text/plain'}));input.files=transfer.files;document.getElementById('import').click();})()`);
  await until("document.getElementById('count').textContent === '1 passages'");
  assert.equal(await evaluate("document.querySelectorAll('.passage img').length"),0);
  assert.match(await evaluate("document.querySelector('.passage').textContent"),/quiet walk/);

  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true);
  const exportData = {Activity: [{SearchTerm:'gardens'}, {Hashtag:'#plants'},
    {Comment:'Quiet mornings'}, {Date:'1970-01-01 00:00:00', Link:'https://www.tiktok.com/video/1'},
    {Interests:['Gardening','Gardening']}]};
  const request = {source:'tiktok', files:[{name:'export.json', text:JSON.stringify(exportData)}]};
  const payload = JSON.stringify(JSON.stringify(request));
  const imported = await evaluate(`JSON.parse(SaintAndroid.request('/api/import', ${payload}))`);
  assert.equal(imported.ok,true);
  let nativeState=await evaluate("JSON.parse(SaintAndroid.request('/api/state','{}'))");
  assert.equal(nativeState.records.length,3);
  assert.equal(nativeState.watches,1);
  assert.deepEqual(nativeState.categories,['Gardening']);
  const invalid = JSON.stringify(JSON.stringify({source:'notes',files:[{name:'bad.pdf',text:'wrong type'}]}));
  assert.ok((await evaluate(`JSON.parse(SaintAndroid.request('/api/import', ${invalid}))`)).error);
  nativeState=await evaluate("JSON.parse(SaintAndroid.request('/api/state','{}'))");
  assert.equal(nativeState.records.length,3);

  await evaluate("document.getElementById('clear').click()");
  await until("document.getElementById('count').textContent === '0 passages'");
  assert.equal(await evaluate("document.getElementById('empty').hidden"),false);
  console.log('Android smoke passed: native bridge, mobile sources, sample, search, note import, TikTok parsing, failed-import recovery, escaping, layout, clear.');
} finally { ws?.close(); }
