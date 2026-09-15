'use strict';
const $ = id => document.getElementById(id);
let state, token, limit = 40, poll, hits = [], busy = false, autoAnalyzing = false, visible = null;
const NO_FILTERS = {origin: null, island: null, signal: null}, FILTER_GROUPS = Object.keys(NO_FILTERS);
let filters = {...NO_FILTERS};
// The last import's outcome stays in front of later analysis progress messages, so a note like
// "kept the most recent passages" isn't replaced by "Finding semantic islands…" a moment later.
let notice = '';
const withNotice = message => notice ? `${notice} ${message}` : message;
const islandColor = label => label < 0 ? '#acb1a8' : ['#365944','#92a779','#c77b59','#7d8da6','#ac8ba5','#b7a65b'][label % 6];
// File-based sources: accept pattern + file-input label. Missing entries (firefox,
// chrome) read a local browser profile server-side instead of taking an upload.
const SOURCES = {
  auto: {label: 'Any export file — source detected automatically', accept: '.md,.markdown,.txt,.json,.js,.csv'},
  notes: {label: 'Markdown or plain-text files', accept: '.md,.markdown,.txt'},
  tiktok: {label: 'TikTok JSON export files', accept: '.json'},
  youtube: {label: 'YouTube Takeout JSON files (watch-history.json, search-history.json)', accept: '.json'},
  instagram: {label: 'Instagram "Download your information" JSON files', accept: '.json'},
  x: {label: 'X/Twitter export .js files (e.g. search-history.js)', accept: '.js'},
  spotify: {label: 'Spotify extended streaming history JSON files', accept: '.json'},
  reddit: {label: 'Reddit posts.csv / comments.csv files', accept: '.csv'},
  amazon: {label: 'Amazon order history CSV (Retail.OrderHistory.*.csv)', accept: '.csv'},
  usage: {label: 'A usage.json screen-time file ({app, minutes, date} rows)', accept: '.json'},
};
const SOURCE_NAMES = {notes: 'personal notes', tiktok: 'TikTok export', youtube: 'YouTube history',
  instagram: 'Instagram export', x: 'X/Twitter export', spotify: 'Spotify history',
  reddit: 'Reddit export', amazon: 'Amazon order history', usage: 'device screen time',
  firefox: 'Firefox history', chrome: 'Chrome history', demo: 'sample journal'};
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
  const counts = Object.fromEntries(FILTER_GROUPS.map(g => [g, new Map()])), selected = [];
  state.records.forEach((r, index) => {
    if (!`${r.text} ${r.detail}`.toLowerCase().includes(q)) return;
    const point = state.map?.points[index], values = {origin: r.origin, island: point?.label, signal: point?.signal};
    const misses = FILTER_GROUPS.filter(g => filters[g] !== null && values[g] !== filters[g]);
    // Faceted counts: a chip counts the passages that would show if it were picked, given
    // the search and every other group's current filter.
    FILTER_GROUPS.forEach(g => { if (!misses.length || (misses.length === 1 && misses[0] === g)) counts[g].set(values[g], (counts[g].get(values[g]) || 0) + 1); });
    if (!misses.length) selected.push({...r, index});
  });
  visible = new Set(selected.map(r => r.index));
  renderFilters(counts);
  $('records').replaceChildren(...selected.slice(0,limit).map(r => {
    const card = node('article','','passage');
    const point = state.map?.points[r.index];
    const island = point ? ` · ${point.label < 0 ? 'unclustered' : `island ${point.label}`} · outlier score ${point.outlier?.toFixed(2) ?? 'unavailable'} · ${point.signal ? 'signal' : 'flagged as noise'}` : '';
    card.append(node('small', `${r.detail} · ${r.source}${island}`),node('p',r.text)); return card;
  }));
  if(!selected.length) $('records').append(node('p','No matching passages.','muted'));
  $('more').hidden = selected.length <= limit;
  $('showing').hidden = !q && FILTER_GROUPS.every(g => filters[g] === null);
  $('showing').textContent = `Showing ${selected.length} of ${state.records.length} passages`;
  draw();
}
function renderFilters(counts) {
  const groups = [];
  if ((state.sources || []).length > 1) groups.push(['origin', 'Source', state.sources.map(s => [s.source, SOURCE_NAMES[s.source] || s.source])]);
  if (state.map) {
    const labels = [...new Set(state.map.points.map(p => p.label))].sort((a, b) => (a < 0) - (b < 0) || a - b);
    groups.push(['island', 'Island', labels.map(l => [l, l < 0 ? 'unclustered' : `island ${l}`])]);
    groups.push(['signal', 'Score', [[true, 'signal'], [false, 'flagged as noise']]]);
  }
  const focused = document.activeElement?.dataset?.filter;
  $('filters').hidden = !groups.length;
  $('filters').replaceChildren(...groups.map(([group, title, options]) => {
    const row = node('div', '', 'filter-group');
    row.append(node('span', title));
    options.forEach(([value, text]) => {
      const count = counts[group].get(value) || 0, chip = node('button', `${text} · ${count}`, 'chip');
      chip.type = 'button'; chip.dataset.filter = `${group}:${value}`;
      chip.setAttribute('aria-pressed', String(filters[group] === value));
      chip.classList.toggle('empty', !count);
      if (group === 'island') { chip.classList.add('swatch'); chip.style.setProperty('--swatch', islandColor(value)); }
      chip.onclick = () => { filters[group] = filters[group] === value ? null : value; limit = 40; passages(); };
      row.append(chip);
    });
    return row;
  }));
  if (FILTER_GROUPS.some(g => filters[g] !== null)) {
    const reset = node('button', 'Clear filters', 'quiet');
    reset.type = 'button';
    reset.onclick = () => { filters = {...NO_FILTERS}; limit = 40; passages(); $('search').focus(); };
    $('filters').append(reset);
  }
  // Rebuilding the chips would otherwise drop keyboard focus after every toggle.
  if (focused) [...$('filters').querySelectorAll('button')].find(b => b.dataset.filter === focused)?.focus();
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
    ctx.fillStyle=i>=pts.length?'#b58a32':islandColor(p.label);
    // Passages hidden by the current search or filters fade back so the matching ones stand out.
    ctx.globalAlpha=i<pts.length&&visible&&!visible.has(i)?0.15:1;
    ctx.beginPath();
    if(i>=pts.length){for(let j=0;j<10;j++){const a=j*Math.PI/5-Math.PI/2,r=j%2?4:10;ctx.lineTo(x+Math.cos(a)*r,y+Math.sin(a)*r);}ctx.closePath();}
    else ctx.arc(x,y,5,0,2*Math.PI);
    ctx.fill();ctx.globalAlpha=1;hits.push({x,y,text:i<pts.length?`${state.records[i].detail}: ${state.records[i].text}`:`Assigned label: ${p.text}. Closest passage (cosine similarity ${p.similarity.toFixed(2)}): ${p.nearest}`});
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
  $('sources-row').hidden=!state.sources?.length;
  $('sources-row').replaceChildren(...(state.sources||[]).map(s=>node('span',`${SOURCE_NAMES[s.source]||s.source} · ${s.count}`,'chip')));
  $('analyze').disabled=busy||state.records.length<30||state.job.status==='running'||!state.semantic_installed;
  $('model-help').textContent=!state.semantic_installed?'Optional setup: run python setup_local.py, then python setup_local.py --download-model. Restart using python run.py.': 'Uses your locally cached model. At least 30 passages required. First analysis can take several minutes.';
  $('map-wrap').hidden=!state.map;
  if(!(state.sources||[]).some(s=>s.source===filters.origin))filters.origin=null;
  if(!state.map)filters.island=filters.signal=null;
  const health=state.map?.health;
  $('health').hidden=!health;
  if(health){
    $('health-snr').textContent=`${Math.round(health.snr*100)}%`;
    $('health-homog').textContent=`${Math.round(health.homogenization*100)}%`;
    $('health-score').textContent=`${Math.round(health.profile_health*100)}%`;
  }
  $('categories').replaceChildren();
  if(state.categories.length) $('categories').append(node('h3','Platform-assigned labels'),node('p',state.categories.join(' · ')));
  if (state.platform === 'android') {
    $('analyze').parentElement.hidden = true;
    $('model-help').textContent = 'Semantic maps are available in the desktop app. Here you can explore your passages and recurring words offline.';
  }
  passages();
  maybeAutoAnalyze();
}
// Auto-run the semantic map once there's enough data and none is pending/failed yet.
// state.map is already null right after any import and only becomes non-null after a
// successful analyze, so this naturally re-fires after every new import and stays quiet
// while a job is running or has errored (job.status is then 'running'/'error', not 'idle').
// Deliberately NOT routed through action()/busy: that mutex disables every button for the
// duration, which is right for a user-initiated click but wrong for an invisible background
// trigger -- and a request that loses a race with an in-flight analysis from a prior import
// (409, "still running") must back off quietly rather than retry in a tight synchronous loop
// that starves out the Clear button.
function maybeAutoAnalyze() {
  if (autoAnalyzing || busy) return;
  if (!(state.records.length >= 30 && !state.map && state.job.status === 'idle' && state.semantic_installed)) return;
  autoAnalyzing = true;
  (async () => {
    try { await api('/api/analyze', {}); await refresh(); }
    catch { await new Promise(r => setTimeout(r, 2000)); }
    finally { autoAnalyzing = false; }
  })();
}
async function refresh() {
  state=await api('/api/state'); token=state.token; render();
  clearTimeout(poll);
  if(state.job.status==='running'){status(withNotice('Finding semantic islands on your machine…'));poll=setTimeout(()=>refresh().catch(e=>status(e.message,true)),1500);}
  else if(state.job.status==='error') status(state.job.error,true);
  else if(state.job.status==='done') status(withNotice('Your semantic map is ready. Explore the passages behind each island.'));
}
async function action(fn) {
  if(busy)return; busy=true; ['import','demo','clear','analyze'].forEach(id=>$(id).disabled=true);
  try { await fn(); } catch(e) {status(e.message,true);} finally {busy=false;['import','demo','clear'].forEach(id=>$(id).disabled=false);if(state)render();}
}
$('source').addEventListener('change',()=>{
  const cfg=SOURCES[$('source').value];
  $('file-area').hidden=!cfg;$('browser-help').hidden=!!cfg;$('files').value='';
  if(cfg){$('files').accept=cfg.accept;$('file-label').textContent=cfg.label;}
});
// Large selections go up in parts so the size cap applies per file: small files share a part
// up to the cap, and a big export file (Spotify history runs ~12.8 MB) travels alone. Every
// part carries all selected names so auto-detect still checks the whole selection.
const MAX_FILE_BYTES = window.SaintAndroid ? 16000000 : 32000000;
function parts(files) {
  const out = [];
  for (const f of files) {
    const last = out[out.length - 1];
    if (last && last.bytes + f.size <= MAX_FILE_BYTES && last.files.length < 500) { last.files.push(f); last.bytes += f.size; }
    else out.push({files: [f], bytes: f.size});
  }
  return out;
}
const count = (n, one, many) => `${n.toLocaleString()} ${n === 1 ? one : many}`;
function importSummary({changed, added, skipped, dropped}) {
  if (!changed) return dropped ? 'Nothing was added: the session is at its 5,000-passage limit and keeps the most recent entries. Clear it to import older ones.' : 'Everything in these files is already in your session.';
  if (!added) return 'Added to your session.';
  return `Added ${count(added, 'passage', 'passages')}${skipped ? ` (${skipped.toLocaleString()} already in your session)` : ''}.`
    + (dropped ? ` Kept the most recent; ${count(dropped, 'older entry', 'older entries')} didn't fit the 5,000-passage session.` : ' Explore your passages below.');
}
$('import').onclick=()=>action(async()=>{
  const source=$('source').value, chosen=[...$('files').files];
  if(SOURCES[source]&&!chosen.length)throw new Error('Choose files to import first.');
  const tooBig=chosen.find(f=>f.size>MAX_FILE_BYTES);
  if(tooBig)throw new Error(`${tooBig.name} is larger than ${MAX_FILE_BYTES/1e6} MB. Choose a smaller export file.`);
  const groups=SOURCES[source]?parts(chosen):[{files:[]}], names=chosen.map(f=>f.name), errors=[];
  const totals={changed:false,added:0,skipped:0,dropped:0};
  let detected;
  for(const [i,group] of groups.entries()){
    status(groups.length>1?`Reading part ${i+1} of ${groups.length} of your selection…`:'Reading your selected source…');
    try{
      const files=await Promise.all(group.files.map(async f=>({name:f.name,text:await f.text()})));
      const result=await api('/api/import',{source,files,names});
      detected=detected||result.detected_source;
      totals.changed=totals.changed||result.changed!==false;
      for(const key of ['added','skipped','dropped'])totals[key]+=result[key]||0;
    }catch(e){errors.push(e.message);}
  }
  if(errors.length===groups.length)throw new Error(errors[0]);
  const prefix=source==='auto'&&detected?`Detected ${SOURCE_NAMES[detected]||detected}. `:'';
  const failures=errors.length?` ${errors.length} of ${groups.length} parts couldn't be imported: ${errors[0]}`:'';
  notice=prefix+importSummary(totals)+failures;
  $('point-detail').textContent='Hover or click a point to read its passage.';limit=40;$('search').value='';filters={...NO_FILTERS};await refresh();
  if(state.job.status==='idle')status(notice, errors.length>0);
});
$('demo').onclick=()=>action(async()=>{const result=await api('/api/import',{source:'demo'});notice=result.changed===false?(result.dropped?importSummary(result):'The sample journal is already in your session.'):'Sample journal loaded. These are synthetic passages.';limit=40;$('search').value='';filters={...NO_FILTERS};await refresh();if(state.job.status==='idle')status(notice);});
$('clear').onclick=()=>action(async()=>{await api('/api/clear',{});notice='';$('files').value='';$('search').value='';$('point-detail').textContent='Hover or click a point to read its passage.';await refresh();status(window.SaintAndroid ? 'Session cleared.' : 'Session cleared. A running analysis releases its memory when it finishes.');});
$('analyze').onclick=()=>action(async()=>{notice='';await api('/api/analyze',{});await refresh();});
$('search').oninput=()=>{limit=40;passages();};$('more').onclick=()=>{limit+=40;passages();};
function inspect(event){const rect=$('map').getBoundingClientRect(),x=(event.clientX-rect.left)*900/rect.width,y=(event.clientY-rect.top)*380/rect.height;const closest=hits.reduce((best,p)=>{const d=Math.hypot(p.x-x,p.y-y);return d<best.d?{d,p}:best;},{d:22});if(closest.p)$('point-detail').textContent=closest.p.text;}
$('map').onmousemove=inspect;$('map').onclick=inspect;
if (window.SaintAndroid) {
  document.querySelectorAll('#source option[value="firefox"], #source option[value="chrome"]').forEach(option=>option.remove());
  document.querySelector('.local').textContent = '● On this phone';
  $('file-help').textContent = 'Up to 16 MB per file and 1 MB per note. New imports add to this session, which holds up to 5,000 passages.';
  document.querySelector('.privacy p').textContent = 'Selected text stays in this app’s memory. Clear the session when you’re done. Android may also clear the session when it closes the app.';
}
refresh().catch(e=>status(`Cannot reach the local app: ${e.message}`,true));
