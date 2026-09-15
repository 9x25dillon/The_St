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

  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true);

  // Bridge-level checks bypass the file picker: feed {source, files} straight to
  // SaintAndroid.request, same pattern for every adapter, each starting from a clean slate.
  await evaluate("SaintAndroid.request('/api/clear','{}')");
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

  // YouTube adapter, and auto-detect by filename, through the bridge directly.
  await evaluate("SaintAndroid.request('/api/clear','{}')");
  const ytEntries = [
    {title:'Searched for gardening tips', time:'2024-01-01T00:00:00Z'},
    {title:'Watched How to prune roses', titleUrl:'https://www.youtube.com/watch?v=abc', time:'2024-01-02T00:00:00Z'},
    {title:'Watched a video that has been removed', time:'2024-01-03T00:00:00Z'},
  ];
  const ytRequest = {source:'auto', files:[{name:'watch-history.json', text:JSON.stringify(ytEntries)}]};
  const ytPayload = JSON.stringify(JSON.stringify(ytRequest));
  const ytImported = await evaluate(`JSON.parse(SaintAndroid.request('/api/import', ${ytPayload}))`);
  assert.equal(ytImported.ok,true);
  assert.equal(ytImported.detected_source,'youtube');
  nativeState = await evaluate("JSON.parse(SaintAndroid.request('/api/state','{}'))");
  assert.equal(nativeState.records.length,2);
  assert.deepEqual(nativeState.records.map(r=>r.source).sort(),['search','watch']);

  // Remaining adapters, bridge-level, each starting from a clean slate.
  const cases = [
    {source:'instagram', name:'your_topics.json',
     text:JSON.stringify({topics_your_topics:[{string_map_data:{Name:{value:'Cooking'}}}]}),
     expectCategories:['Cooking']},
    {source:'spotify', name:'Streaming_History_Audio_1.json',
     text:JSON.stringify([{ts:'2024-01-01T00:00:00Z',master_metadata_track_name:'A Song',
       master_metadata_album_artist_name:'A Band'}]),
     expectText:'A Song — A Band'},
    {source:'reddit', name:'posts.csv',
     text:'id,permalink,date,ip,subreddit,gildings,title,url,body\n'
       +'1,/r/x/1,2024-01-01 00:00:00 UTC,0.0.0.0,gardening,0,Tomato tips,,Water deeply\n',
     expectText:'Tomato tips. Water deeply'},
    {source:'amazon', name:'Retail.OrderHistory.1.csv',
     text:'Order Date,Product Name\n2024-01-01 00:00:00 UTC,A Nice Lamp\n',
     expectText:'A Nice Lamp'},
    {source:'usage', name:'usage.json',
     text:JSON.stringify([{app:'Instagram',minutes:10,date:'2024-01-01'}]),
     expectText:'Instagram: 10 minutes'},
    {source:'x', name:'search-history.js',
     text:'window.YTD.search_history.part0 = '+JSON.stringify([{searchHistory:{query:'gardening tips'}}])+';',
     expectText:'gardening tips'},
  ];
  for (const c of cases) {
    await evaluate("SaintAndroid.request('/api/clear','{}')");
    const req = {source:c.source, files:[{name:c.name, text:c.text}]};
    const reqPayload = JSON.stringify(JSON.stringify(req));
    const res = await evaluate(`JSON.parse(SaintAndroid.request('/api/import', ${reqPayload}))`);
    assert.equal(res.ok, true, `${c.source} import failed: ${res.error}`);
    assert.equal(res.detected_source, c.source, c.source);
    const st = await evaluate("JSON.parse(SaintAndroid.request('/api/state','{}'))");
    if (c.expectText) assert.equal(st.records[0].text, c.expectText, c.source);
    if (c.expectCategories) assert.deepEqual(st.categories, c.expectCategories, c.source);
  }

  await evaluate("document.getElementById('clear').click()");
  await until("document.getElementById('count').textContent === '0 passages'");
  assert.equal(await evaluate("document.getElementById('empty').hidden"),false);
  console.log('Android smoke passed: native bridge, mobile sources, sample, search, note import, auto-detect, combined sessions, TikTok/YouTube/Instagram/X/Spotify/Reddit/Amazon/usage parsing, failed-import recovery, escaping, layout, clear.');
} finally { ws?.close(); }
