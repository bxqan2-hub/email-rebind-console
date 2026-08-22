const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
let STATE = {accounts:[], replacements:[], tasks:[], summary:{}};
let selected = new Set();
let pairs = [];
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
function toast(message){const el=$('#toast');el.textContent=message;el.classList.add('show');clearTimeout(toast.t);toast.t=setTimeout(()=>el.classList.remove('show'),2200)}
async function api(url, options={}){const response=await fetch(url, options);const data=await response.json().catch(()=>({}));if(!response.ok||data.ok===false)throw new Error(data.error||`HTTP ${response.status}`);return data}
function pill(status){return `<span class="pill ${esc(status)}">${esc(({ready:'待换绑',queued:'排队中',running:'换绑中',success:'已完成',failed:'失败',available:'可用',reserved:'已占用',used:'已使用'})[status]||status||'-')}</span>`}
function renderStats(){const s=STATE.summary||{};$('#stats').innerHTML=[['已导入账号',s.accounts_total||0],['待换绑',s.accounts_ready||0],['换绑完成',s.accounts_success||0],['替换邮箱可用',s.replacement_available||0],['活动任务',s.tasks_active||0]].map(([k,v])=>`<div class="stat"><span>${k}</span><strong>${v}</strong></div>`).join('')}
function pairFor(id){return pairs.find(item=>Number(item.account_id)===Number(id))}
function renderPairs(){const eligible=STATE.accounts.filter(a=>['ready','failed'].includes(a.status));const body=$('#pairBody');body.innerHTML=eligible.map(a=>{const p=pairFor(a.id);return `<tr><td><input type="checkbox" data-account-id="${a.id}" ${selected.has(Number(a.id))?'checked':''}></td><td><strong>${esc(a.old_email)}</strong></td><td>${pill(a.status)}</td><td>${p?`<strong>${esc(p.new_email)}</strong><small> · 替换邮箱 #${p.replacement_id}</small>`:'<span class="pill failed">号池不足</span>'}</td></tr>`}).join('')||'<tr><td colspan="4">暂无待换绑账号，请先导入主站账号。</td></tr>';body.querySelectorAll('[data-account-id]').forEach(el=>el.onchange=()=>{const id=Number(el.dataset.accountId);el.checked?selected.add(id):selected.delete(id);preview()});$('#selectAll').checked=eligible.length>0&&eligible.every(a=>selected.has(Number(a.id)))}
function renderPool(){$('#poolBody').innerHTML=STATE.replacements.map(r=>`<tr><td>#${r.id}</td><td><strong>${esc(r.email)}</strong></td><td>${r.has_api?'已配置':'缺失'}</td><td>${pill(r.status)}</td><td>${esc(r.bound_old_email||'-')}</td></tr>`).join('')||'<tr><td colspan="5">号池为空</td></tr>'}
function taskRows(target){target.innerHTML=STATE.tasks.map(t=>`<tr><td>#${t.id}</td><td><strong>${esc(t.old_email)}</strong> → ${esc(t.new_email)}</td><td>${pill(t.status)}</td><td>${esc(t.stage||'-')}</td><td title="${esc(t.message||'')}">${esc((t.message||'-').slice(0,90))}</td><td>${esc(t.completed_at||'-')}</td></tr>`).join('')||'<tr><td colspan="6">暂无任务</td></tr>'}
function renderRecent(){const rows=STATE.tasks.slice(0,8);$('#recentTasks').innerHTML=rows.map(t=>`<div class="task-row"><strong>#${t.id}</strong><div>${esc(t.old_email)}<br><small>→ ${esc(t.new_email)}</small></div><div>${pill(t.status)}</div><div>${esc(t.message||t.stage||'-')}</div></div>`).join('')||'<div class="task-row"><small>暂无换绑任务</small></div>'}
function render(){renderStats();renderPairs();renderPool();taskRows($('#taskBody'));renderRecent()}
async function load(){try{STATE=await api('/api/state');const valid=new Set(STATE.accounts.filter(a=>['ready','failed'].includes(a.status)).map(a=>Number(a.id)));selected=new Set([...selected].filter(id=>valid.has(id)));if(!selected.size)valid.forEach(id=>selected.add(id));await preview(false);render()}catch(e){toast('加载失败：'+e.message)}}
async function preview(doRender=true){try{const r=await api('/api/pairs/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_ids:[...selected]})});pairs=r.pairs||[];if(doRender)renderPairs()}catch(e){toast('配对失败：'+e.message)}}
async function importText(url, selector){const text=$(selector).value;if(!text.trim())return toast('请先粘贴内容');try{const r=await api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});$(selector).value='';toast(`识别 ${r.parsed} 行，新增 ${r.inserted}，更新 ${r.updated}`);await load()}catch(e){toast(e.message)}}
$('#importAccounts').onclick=()=>importText('/api/accounts/import','#accountImport');
$('#importReplacements').onclick=()=>importText('/api/replacements/import','#replacementImport');
$('#previewPairs').onclick=()=>preview();
$('#selectAll').onchange=e=>{const ids=STATE.accounts.filter(a=>['ready','failed'].includes(a.status)).map(a=>Number(a.id));selected=e.target.checked?new Set(ids):new Set();preview()};
$('#startRebind').onclick=async()=>{if(!selected.size)return toast('请至少选择一个待换绑账号');if(!pairs.length)return toast('没有可用的一对一配对');if(!confirm(`将启动 ${pairs.length} 个 Roxy 换绑任务，确定继续？`))return;const button=$('#startRebind');button.disabled=true;try{const r=await api('/api/rebind/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_ids:[...selected],workers:Number($('#workers').value||2)})});toast(`已提交 ${r.submitted} 个换绑任务`);selected.clear();await load()}catch(e){toast(e.message)}finally{button.disabled=false}};
$('#copyExport').onclick=async()=>{const r=await fetch('/api/export');const text=await r.text();if(!text.trim())return toast('暂无完成结果');await navigator.clipboard.writeText(text);toast('已复制换绑完成结果')};
$('#refreshTasks').onclick=load;
$$('.nav').forEach(button=>button.onclick=()=>{$$('.nav').forEach(x=>x.classList.toggle('active',x===button));$$('.view').forEach(v=>v.classList.add('hidden'));$(`#view-${button.dataset.view}`).classList.remove('hidden')});
load();setInterval(load,3000);

