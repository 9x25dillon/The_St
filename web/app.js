'use strict';
const $ = id => document.getElementById(id);
let state, token, limit = 40, poll, hits = [], busy = false;
function status(message, error = false) { $('status').textContent = message; $('status').classList.toggle('error', error); }
async function api(path, data) {
  if (window.SaintAndroid) {
    const result = JSON.parse(window.SaintAndroid.request(path, JSON.stringify(data ?? {})));
    if (result.error) throw new Error(result.error);
    return result;
  }
  const response = await fetch(path, data === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Saint-Token':token}, body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed.');
  return result;
}
function node(tag, text, cls) { const el = document.createElement(tag); el.textContent = text; if(cls) el.className = cls; return el; }
function passages() {
  const q = $('search').value.toLowerCase();
  const selected = state.records.map((r,i) => ({...r,index:i})).filter(r => `${r.text} ${r.detail}`.toLowerCase().includes(q));
  $('records').replaceChildren(...selected.slice(0,limit).map(r => {
    const card = node('article','','passage');
    const point = state.map?.points[r.index];
    const island = point ? ` · ${point.label < 0 ? 'noise' : `island ${point.label}`} · outlier score ${point.outlier?.toFixed(2) ?? 'unavailable'}` : '';
    card.append(node('small', `${r.detail} · ${r.source}${island}`),node('p',r.text)); return card;
  }));
  if(!selected.length) $('records').append(node('p','No matching passages.','muted'));
  $('more').hidden = selected.length <= limit;
}
function draw() {
  if (!state?.map) return;
  const canvas = $('map'), ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  const pts = state.map.points, stars = state.map.stars, all = [...pts,...stars];
  const xs=all.map(p=>p.x), ys=all.map(p=>p.y), minX=Math.min(...xs), maxX=Math.max(...xs), minY=Math.min(...ys), maxY=Math.max(...ys);
  ctx.clearRect(0,0,w,h); hits=[];
  all.forEach((p,i)=>{
    const x=30+(p.x-minX)/(maxX-minX||1)*(w-60), y=30+(p.y-minY)/(maxY-minY||1)*(h-60);
    ctx.fillStyle=i>=pts.length?'#b58a32':p.label<0?'#acb1a8':['#365944','#92a779','#c77b59','#7d8da6','#ac8ba5','#b7a65b'][p.label%6];
    ctx.beginPath();
    if(i>=pts.length){for(let j=0;j<10;j++){const a=j*Math.PI/5-Math.PI/2,r=j%2?4:10;ctx.lineTo(x+Math.cos(a)*r,y+Math.sin(a)*r);}ctx.closePath();}
    else ctx.arc(x,y,5,0,2*Math.PI);
    ctx.fill();hits.push({x,y,text:i<pts.length?`${state.records[i].detail}: ${state.records[i].text}`:`Assigned label: ${p.text}. Closest passage (cosine similarity ${p.similarity.toFixed(2)}): ${p.nearest}`});
  });
}
function render() {
  const hasData=state.records.length||state.categories.length||state.watches;
  $('empty').hidden=!!hasData; $('content').hidden=!hasData;
  $('result-title').textContent=hasData?'Threads of your everyday':'Room for your thoughts';
  $('count').textContent=`${state.records.length} passages`;
  $('file-count').textContent=new Set(state.records.map(r=>r.detail)).size;
  $('category-count').textContent=state.categories.length; $('watch-count').textContent=state.watches;
  $('terms').replaceChildren(...state.terms.map(([word,count])=>node('span',`${word} · ${count}`,'chip')));
  $('analyze').disabled=busy||state.records.length<30||state.job.status==='running'||!state.semantic_installed;
  $('model-help').textContent=!state.semantic_installed?'Optional setup: run python setup_local.py, then python setup_local.py --download-model. Restart using python run.py.': 'Uses your locally cached model. At least 30 passages required. First analysis can take several minutes.';
  $('map-wrap').hidden=!state.map;
  $('categories').replaceChildren();
  if(state.categories.length) $('categories').append(node('h3','Platform-assigned labels'),node('p',state.categories.join(' · ')));
  if (state.platform === 'android') {
    $('analyze').parentElement.hidden = true;
    $('model-help').textContent = 'Semantic maps are available in the desktop app. Here you can explore your passages and recurring words offline.';
  }
  passages(); draw();
}
async function refresh() {
  state=await api('/api/state'); token=state.token; render();
  clearTimeout(poll);
  if(state.job.status==='running'){status('Finding semantic islands on your machine…');poll=setTimeout(()=>refresh().catch(e=>status(e.message,true)),1500);}
  else if(state.job.status==='error') status(state.job.error,true);
  else if(state.job.status==='done') status('Your semantic map is ready. Explore the passages behind each island.');
}
async function action(fn) {
  if(busy)return; busy=true; ['import','demo','clear','analyze'].forEach(id=>$(id).disabled=true);
  try { await fn(); } catch(e) {status(e.message,true);} finally {busy=false;['import','demo','clear'].forEach(id=>$(id).disabled=false);if(state)render();}
}
$('source').addEventListener('change',()=>{
  const notes=$('source').value==='notes', files=notes||$('source').value==='tiktok';
  $('file-area').hidden=!files;$('browser-help').hidden=files;$('files').value='';
  $('files').accept=notes?'.md,.markdown,.txt':'.json';$('file-label').textContent=notes?'Markdown or plain-text files':'TikTok JSON export files';
});
$('import').onclick=()=>action(async()=>{
  const source=$('source').value, chosen=[...$('files').files];
  if(['notes','tiktok'].includes(source)&&!chosen.length)throw new Error('Choose files to import first.');
  if(chosen.reduce((n,f)=>n+f.size,0)>7500000)throw new Error('Choose a smaller import (under 8 MB).');
  status('Reading your selected source…');
  const files=await Promise.all(chosen.map(async f=>({name:f.name,text:await f.text()})));
  await api('/api/import',{source,files});$('point-detail').textContent='Hover or click a point to read its passage.';limit=40;$('search').value='';await refresh();status('Imported locally. Explore your passages below.');
});
$('demo').onclick=()=>action(async()=>{await api('/api/import',{source:'demo'});limit=40;$('search').value='';await refresh();status('Sample journal loaded. These are synthetic passages.');});
$('clear').onclick=()=>action(async()=>{await api('/api/clear',{});$('files').value='';$('search').value='';$('point-detail').textContent='Hover or click a point to read its passage.';await refresh();status(window.SaintAndroid ? 'Session cleared.' : 'Session cleared. A running analysis releases its memory when it finishes.');});
$('analyze').onclick=()=>action(async()=>{await api('/api/analyze',{});await refresh();});
$('search').oninput=()=>{limit=40;passages();};$('more').onclick=()=>{limit+=40;passages();};
function inspect(event){const rect=$('map').getBoundingClientRect(),x=(event.clientX-rect.left)*900/rect.width,y=(event.clientY-rect.top)*380/rect.height;const closest=hits.reduce((best,p)=>{const d=Math.hypot(p.x-x,p.y-y);return d<best.d?{d,p}:best;},{d:22});if(closest.p)$('point-detail').textContent=closest.p.text;}
$('map').onmousemove=inspect;$('map').onclick=inspect;
if (window.SaintAndroid) {
  document.querySelectorAll('#source option[value="firefox"], #source option[value="chrome"]').forEach(option=>option.remove());
  document.querySelector('.local').textContent = '● On this phone';
  document.querySelector('.privacy p').textContent = 'Selected text stays in this app’s memory. Clear the session when you’re done. Android may also clear the session when it closes the app.';
}
refresh().catch(e=>status(`Cannot reach the local app: ${e.message}`,true));
