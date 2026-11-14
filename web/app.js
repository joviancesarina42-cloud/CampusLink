'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {native6:'原生 IPv6',tunnel6:'隧道 IPv6',ipv4:'公网 IPv4',ipv6:'公网 IPv6',unknown:'无法确认',lan:'局域网',outer:'隧道外层',blocked:'已阻断'};
const routes = {airport:'默认出口组',ai:'AI 服务组',warp:'IPv6 WARP',native:'原生 IPv6',ipv4:'允许 IPv4',block:'阻断'};
const colors = {native6:'green',tunnel6:'green',ipv6:'green',ipv4:'amber',unknown:'muted',lan:'muted',outer:'muted',blocked:'red'};
// 网络层异常换成可读的一句话，避免把 WinError 原文丢到面板上。
const shortError = e => !e ? '' : /WinError|refused|积极拒绝|timed out|超时|getaddrinfo/i.test(e) ? '尚未建立可验证的出口；开启链路后会自动复测' : e;
let token='', snapshot=null, plane='live', formLoaded=false, exceptions=[], job=null, toastTimer=null;
let nodeDirty=false,nodeCatalogKey='';
let activePage='overview',pollTimer=null,polling=false,hostHidden=false,pollAgain=false;
const textFields=['mode','airport_ipv6','api','warp_udp','source_config','kernel_path','warp_path'];
const numberFields=['socks_port','mixed_port','core_api_port','check_seconds','failure_threshold'];
const listFields=['endpoints','dns','campus_domains','campus_cidrs','native_domains','warp_domains','warp_processes','warp_cidrs'];
const checkFields=['native_cn','warp_enable','tun','auto_capture'];

function bytes(n){
  n=Number(n)||0;
  const units=['B','KiB','MiB','GiB','TiB'];let i=0;
  while(n>=1024&&i<4){n/=1024;i++;}
  return `${n.toLocaleString('zh-CN',{maximumFractionDigits:i?2:0})} ${units[i]}`;
}
function toast(message){$('toast').textContent=message;$('toast').classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),8500);}
function page(name){
  activePage=name;
  document.querySelectorAll('.page').forEach(x=>x.classList.toggle('active',x.id===name));
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('active',x.dataset.page===name));
  const titles={overview:'链路概览',nodes:'节点分流',traffic:'流量与连接',settings:'策略与配置',activity:'记录与恢复'};
  $('page-title').textContent=titles[name];
  $('subtitle').textContent={overview:'看清每一条链路，优先通过 IPv6 通信。',nodes:'AI 与其他服务分别选择出口。',traffic:'从应用负载到物理网卡，逐条核对真实路径。',settings:'先覆盖更多 IPv6 通信，再按实测调整。',activity:'所有变更都有记录，可恢复到开启前。'}[name];
  if(name==='overview')requestAnimationFrame(chart);
  schedulePoll(0);
}
async function action(name,body={}){
  try{
    if(!token)token=(await(await fetch('/api/token')).json()).token;
    const response=await fetch('/api/'+name,{method:'POST',headers:{'Content-Type':'application/json','X-CampusLink':token},body:JSON.stringify(body)});
    const result=await response.json();
    if(!response.ok)throw Error(result.error||'操作失败');
    job={id:result.job,name};
    $('notice').textContent=({start:'正在校验、建立回滚点并开启链路…',stop:'正在恢复原配置、原 TUN 与系统代理…',recover:'正在恢复上次现场…',preview:'正在使用本机 mihomo 内核校验配置…',import:'正在准备内核与 WARP 账户…','warp-register':'正在注册 WARP 账户…','deps-install':'正在从 GitHub 下载内核与 warpscout…',probe:'正在执行 TLS / HTTPS 探针…',exit:'正在恢复网络并退出…'}[name]||'正在处理…');
    $('notice').classList.remove('hidden');
    schedulePoll(0);
  }catch(e){toast(e.message);}
}
function totals(category,selectedPlane='wire'){
  const item=(snapshot?.totals||[]).filter(x=>x.plane===selectedPlane&&x.category===category);
  return item.reduce((r,x)=>({up:r.up+x.up,down:r.down+x.down,total:r.total+x.up+x.down}),{up:0,down:0,total:0});
}
function pill(category){return `<span class="pill ${colors[category]||'muted'}">${esc(labels[category]||category)}</span>`;}
function fillForm(s){
  [...textFields,...numberFields].forEach(k=>$(k).value=s[k]??'');
  listFields.forEach(k=>$(k).value=(s[k]||[]).join('\n'));
  checkFields.forEach(k=>$(k).checked=!!s[k]);
  $('capture_interfaces').value=(s.capture_interfaces||[]).join(', ');
  exceptions=structuredClone(s.exceptions||[]);
  renderExceptions();
  $('airport-source').textContent=s.airport_source_name?('IPv6 首跳节点：'+s.airport_source_name):'未设置：只用 WARP 与原生 IPv6。';
  formLoaded=true;
}
function formValues(){
  const s=structuredClone(snapshot.settings);
  textFields.forEach(k=>s[k]=$(k).value.trim());
  numberFields.forEach(k=>s[k]=Number($(k).value));
  listFields.forEach(k=>s[k]=$(k).value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean));
  checkFields.forEach(k=>s[k]=$(k).checked);
  s.capture_interfaces=$('capture_interfaces').value.split(/[,，]/).map(x=>x.trim()).filter(Boolean);
  s.exceptions=exceptions;return s;
}
function renderExceptions(){
  $('exceptions').innerHTML=exceptions.length?exceptions.map((e,i)=>`<div class="exception-row"><div>${esc(e.value)}<br><small>${e.kind==='domain'?'域名后缀':'进程'} → ${esc(routes[e.route])}</small></div><button class="text-btn" data-remove="${i}">删除</button></div>`).join(''):'<p class="field-help">没有额外例外；默认不放行公网 IPv4。</p>';
}
function renderNodes(){
  const info=snapshot.nodes||{catalog:[],selected:{}};
  const nodes=info.catalog||[],key=JSON.stringify(nodes.map(n=>[n.id,n.path]));
  const noNodes=!nodes.length;
  if(key!==nodeCatalogKey){
    for(const role of ['ai','default']){
      const current=nodeDirty?$('node-'+role).value:info.selected[role];
      const empty=noNodes?'未配置机场':'原选择不存在，请重新选择';
      $('node-'+role).innerHTML=(!nodes.some(n=>n.id===current)?`<option value="" disabled selected>${empty}</option>`:'')+nodes.map(n=>`<option value="${esc(n.id)}">${esc(n.name)} · ${n.path==='direct6'?'IPv6 直连':'经 IPv6 首跳中转'}</option>`).join('');
      if(nodes.some(n=>n.id===current))$('node-'+role).value=current;
    }
    nodeCatalogKey=key;
  }
  if(!nodeDirty){for(const role of ['ai','default'])$('node-'+role).value=nodes.some(n=>n.id===info.selected[role])?info.selected[role]:'';}
  for(const role of ['ai','default']){
    if(noNodes){
      $('node-'+role+'-route').textContent=info.error||'未配置机场：当前只用 WARP + 原生 IPv6。';
      $('node-'+role+'-status').textContent='未使用';$('node-'+role+'-status').className='pill muted';
      $('node-'+role+'-health').textContent='';
      continue;
    }
    const node=nodes.find(n=>n.id===$('node-'+role).value);
    $('node-'+role+'-route').textContent=node?(node.path==='direct6'?`电脑 → IPv6 → ${node.name}`:`电脑 → IPv6 首跳：${node.gateway} → 出口：${node.name}`):info.error||'原选择已失效，请重新选择或导入节点';
    const h=snapshot.state.node_health?.[role];
    $('node-'+role+'-status').textContent=h?.ok?(h.loc||h.colo)+' · 通过':'待验证';
    $('node-'+role+'-status').className='pill '+(h?.ok?'green':'muted');
    $('node-'+role+'-health').textContent=h?.ok?`${snapshot.state.phase==='running'?'当前已应用':'最近验证'}出口：${h.ip} · ${h.loc||''} / ${h.colo} · ${h.ms} ms`:shortError(h?.error)||'开启链路后可以验证实际出口地区和 IP。';
  }
  const busy=!!snapshot.state.operation;
  $('nodes-apply').disabled=busy||!nodes.length||!$('node-ai').value||!$('node-default').value;
  $('nodes-probe').disabled=busy||snapshot.state.phase!=='running';
  $('nodes-selection-state').textContent=nodeDirty?'选择尚未应用':snapshot.state.phase==='running'?'当前选择已应用于新连接':'当前选择已保存，开启链路后生效';
  $('ai-rule-count').textContent=`${info.ai_rule_count||0} 条域名规则`;
  $('ai-coverage').textContent=noNodes?'未配置机场时 AI 域名走 WARP；配置机场后可按节点分流。':`AI 进程：${(snapshot.settings.ai_processes||[]).join('、')}。`;
}
function renderTable(){
  if(!snapshot)return;
  const search=$('search').value.toLowerCase(),category=$('category').value;
  const live=plane==='live';
  let rows=live?(snapshot.state.connections||[]):(snapshot.top||[]).filter(x=>x.plane===plane);
  rows=rows.filter(x=>(!category||x.category===category)&&(!search||JSON.stringify(x).toLowerCase().includes(search)));
  rows.sort((a,b)=>(b.up+b.down)-(a.up+a.down));
  $('down-heading').textContent=live?'下行 / 次采样':'累计下行';$('up-heading').textContent=live?'上行 / 次采样':'累计上行';
  $('connections').innerHTML=rows.length?rows.slice(0,200).map(x=>`<tr><td><strong>${esc(x.process||(plane==='wire'?'物理网卡观测':'未识别进程'))}</strong><small>${esc(x.host||(plane==='wire'?'外层目标地址':'未知目标'))}</small></td><td>${pill(x.category)}</td><td>${esc(x.remote||'尚无运行时外连证据')}<small>${esc(x.interface||x.reason||'')}</small></td><td>${esc(x.chain||x.protocol||'—')}<small>${esc(x.chain?x.protocol:'')}</small></td><td class="numeric">${bytes(x.down)}</td><td class="numeric">${bytes(x.up)}</td></tr>`).join(''):'<tr><td colspan="6" class="empty">没有符合条件的流量记录</td></tr>';
  $('table-footer').textContent=`${rows.length} 条显示${live?' / '+(snapshot.state.connection_count??rows.length)+' 条当前连接':'累计记录'} · ${snapshot.state.coverage} · 当前连接展示本次流量最高的 200 条，累计表最多 250 个分组 · 近 30 天保留采样明细，更早按日合并`;
}
function chart(){
  if(!snapshot||!$('overview').classList.contains('active'))return;
  const c=$('chart'),ctx=c.getContext('2d'),dpr=devicePixelRatio||1,w=c.clientWidth,h=190;
  if(!w)return;c.width=w*dpr;c.height=h*dpr;ctx.scale(dpr,dpr);
  const data=snapshot.history||[],left=59,right=w-8,top=12,bottom=h-23;
  const maximum=Math.max(1024,...data.map(x=>Math.max(x.ipv4,x.ipv6)))*1.15;
  ctx.font='9px Segoe UI';ctx.textAlign='right';ctx.fillStyle='#a5b3bb';ctx.lineWidth=1;
  for(let i=0;i<4;i++){const y=top+(bottom-top)*i/3;ctx.beginPath();ctx.strokeStyle='#edf2f4';ctx.moveTo(left,y);ctx.lineTo(right,y);ctx.stroke();ctx.fillText(bytes(maximum*(1-i/3))+'/s',left-7,y+3);}
  const points=(key)=>data.map((v,i)=>[left+(right-left)*i/Math.max(89,data.length-1),bottom-(bottom-top)*(v[key]||0)/maximum]);
  for(const [key,color]of[['ipv6','#08a985'],['ipv4','#cf9e49']]){const p=points(key);if(!p.length)continue;ctx.beginPath();ctx.moveTo(p[0][0],bottom);p.forEach(([x,y])=>ctx.lineTo(x,y));ctx.lineTo(p.at(-1)[0],bottom);ctx.closePath();const gradient=ctx.createLinearGradient(0,top,0,bottom);gradient.addColorStop(0,color+'30');gradient.addColorStop(1,color+'04');ctx.fillStyle=gradient;ctx.fill();ctx.beginPath();p.forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.strokeStyle=color;ctx.lineWidth=2;ctx.stroke();}
  ctx.fillStyle='#a5b3bb';ctx.textAlign='left';ctx.fillText('90 次采样前',left,h-5);ctx.textAlign='right';ctx.fillText('现在',right,h-5);
}
function render(){
  const d=snapshot,s=d.state,c=d.capture;
  const busy=!!s.operation;
  ['start','stop','save','campus-save','import','preview','probe','capture','recover','exit','add-exception','deps-install','warp-register'].forEach(k=>$(k).disabled=busy);
  $('start').disabled=busy||['running','recovery','starting','stopping'].includes(s.phase);
  $('stop').disabled=busy||!['running','recovery'].includes(s.phase);
  $('recover').disabled=busy||!['running','recovery'].includes(s.phase);
  $('online').textContent=s.api_online?'内核在线':'内核未连接';$('online').className='pill '+(s.api_online?'green':'muted');
  $('clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});
  const phases={idle:['工具就绪 · 当前为监控状态','点击开启后应用 IPv6 广覆盖策略；仅打开界面不会改动现有链路。'],running:['IPv6 链路已开启',`${s.mode==='standalone'?'独立内核运行':'接管现有内核'} · 原生 IPv6 / WARP / IPv6 机场并行`],starting:['正在开启 IPv6 链路','正在建立回滚点并加载经过校验的配置。'],stopping:['正在恢复原网络','恢复内核、TUN 与系统代理。'],recovery:['检测到上次未完成的恢复','请先恢复上次现场，再开启新链路。']};
  const p=phases[s.phase]||phases.idle;$('phase-title').textContent=p[0];$('phase-description').textContent=p[1];
  const warning=[s.error,s.capture_error,s.storage_error,c.error,s.api_online?'':s.api_error].filter(Boolean).join('\n');
  $('error-banner').textContent=warning;$('error-banner').classList.toggle('hidden',!warning);
  const wireOK=c.started!=null;const v6=totals('ipv6'),v4=totals('ipv4'),lan=totals('lan');
  $('wire-v6').textContent=wireOK||v6.total?bytes(v6.total):'—';$('wire-v4').textContent=wireOK||v4.total?bytes(v4.total):'—';
  $('wire-total').textContent=wireOK||v6.total+v4.total?bytes(v6.total+v4.total):'—';$('wire-lan').textContent=wireOK||lan.total?bytes(lan.total):'—';
  $('wire-share').textContent=`IPv6 占比 ${v6.total+v4.total?(100*v6.total/(v6.total+v4.total)).toFixed(1)+'%':'—'} · 物理 IP 字节`;
  for(const [id,key]of[['v6','ipv6'],['v4','ipv4']]){const r=s.rates[key]||[0,0];$('rate-'+id).textContent=c.running?`↓ ${bytes(r[1])}/s   ↑ ${bytes(r[0])}/s`:'当前未采样速率';}
  $('capture-label').textContent=c.running?`${c.interfaces.map(x=>x.Name).join(' / ')} · 驱动丢包 ${c.dropped}`:s.capture_requested?'物理监控等待网络恢复；恢复后自动重试，等待期间不计数':'物理监控已暂停；暂停期间不计数';
  $('capture').textContent=c.running||s.capture_requested?'暂停监控':'开启监控';
  const warpOn=!!d.settings.warp_enable;
  const wh=warpOn?(s.phase==='running'?d.warp.health:s.existing_warp_health):null;
  $('warp-status').textContent=!warpOn?'已关闭':wh?.ok?'HTTPS 通过':d.warp.status==='connecting'?'连接中':'待验证';$('warp-status').className='pill '+(!warpOn?'muted':wh?.ok?'green':'amber');
  $('warp-detail').textContent=!warpOn?'WARP 未启用：不加载 CL-WARP，也不生成对应规则。':wh?.ok?`${wh.colo} · ${wh.ms} ms · warp=${wh.warp} · ${d.warp.endpoint||s.observed_warp_ipv6||'外层地址待确认'}`:shortError(wh?.error)||'启动后自动探测、重启与更换 IPv6 端点';
  const warpStateEl=$('warp-state');
  if(warpStateEl){warpStateEl.textContent=warpOn?'已启用':'未启用';warpStateEl.className='pill '+(warpOn?'green':'muted');}
  const airportOn=!!d.settings.airport_ipv6;
  $('airport-status').textContent=!airportOn?'未配置':s.health.ok?'HTTPS 通过':'待验证';$('airport-status').className='pill '+(!airportOn?'muted':s.health.ok?'green':'amber');
  $('airport-detail').textContent=!airportOn?'没有设置 IPv6 首跳，流量只走 WARP 与原生 IPv6。':s.health.ok?`${s.health.colo} · ${s.health.ms} ms · 目标出口 ${s.health.ip}`:shortError(s.health.error)||d.settings.airport_ipv6;
  if(activePage==='overview')$('business-summary').innerHTML=['native6','tunnel6','ipv4','unknown'].map(k=>{const n=totals(k,'business');return `<div class="biz-item"><span>${esc(labels[k])}</span><strong>${bytes(n.total)}</strong><small>↓ ${bytes(n.down)}　↑ ${bytes(n.up)}</small></div>`;}).join('');
  const selected=$('day').value||d.day,days=[...new Set([d.day,...(d.days||[])])];
  if(!days.includes(selected))days.unshift(selected);
  $('day').innerHTML=days.map(day=>`<option ${day===selected?'selected':''} value="${esc(day)}">${esc(day)}</option>`).join('');
  $('export').href='/api/export?day='+encodeURIComponent(selected);
  if(!formLoaded)fillForm(d.settings);
  $('recovery-label').textContent=s.phase==='recovery'?'上次现场尚待恢复':s.phase==='running'?'已有开启前的完整回滚点':'没有待恢复的网络变更';
  if(activePage==='activity'){
    $('diagnostics').innerHTML=[['运行权限',s.admin?'管理员':'普通用户'],['本次采样会话',d.session],['最后连接采样',s.sample_time?new Date(s.sample_time*1000).toLocaleTimeString('zh-CN'):'尚无成功采样'],['物理监控',c.running?'运行中':'已暂停'],['物理网卡',(c.interfaces||[]).map(x=>x.Name).join(' / ')||'尚未获取'],['驱动报告丢包',c.dropped],['配置自动重载',s.watchdog_restores+' 次']].map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
    $('events').innerHTML=d.events.length?d.events.map(e=>`<div class="event"><time>${esc(new Date(e.time*1000).toLocaleString('zh-CN',{hour12:false}))}</time><span class="event-kind">${esc(e.kind)}</span><span>${esc(e.message)}</span></div>`).join(''):'<div class="empty">暂无操作记录</div>';
  }
  if(activePage==='traffic')renderTable();
  if(activePage==='overview')chart();
  if(activePage==='nodes')renderNodes();
  if(job&&s.job?.id===job.id&&!s.job.running){
    const {error,result}=s.job;const name=job.name;job=null;
    $('notice').classList.add('hidden');
    if(error)toast(error);
    else if(name==='preview'){$('preview-content').textContent=JSON.stringify(result,null,2);$('preview-panel').classList.remove('hidden');$('preview-panel').scrollIntoView({behavior:'smooth',block:'start'});toast('内核校验通过，覆盖内容已列出。');}
    else if(name==='import'){formLoaded=false;const parts=[result.airport?'机场 '+result.airport:'仅 WARP'];parts.push(result.warp?'WARP 就绪':'WARP 未启用');if(result.notes&&result.notes.length)parts.push(result.notes.join('；'));toast(parts.join(' · '));}
    else if(name==='warp-register'){formLoaded=false;toast(result.message);}
    else if(name==='deps-install'){formLoaded=false;const parts=[result.downloaded&&result.downloaded.length?'下载 '+result.downloaded.join('、'):'依赖已齐全'];parts.push(result.airport?'机场 '+result.airport:'仅 WARP');parts.push(result.warp?'WARP 就绪':'WARP 未启用');toast(parts.join(' · '));}
    else if(name==='probe'){toast(`机场 HTTPS：${result.airport.ok?'通过':result.airport.error}\nWARP HTTPS：${result.warp.ok?'通过':result.warp.error}`);}
    else if(name==='nodes-select'){nodeDirty=false;renderNodes();toast(result.message);}
    else if(name==='nodes-probe'){toast(['ai','default'].map(k=>`${k==='ai'?'AI 服务':'默认出口'}：${result.health[k].ok?(result.health[k].loc+' / '+result.health[k].ip):result.health[k].error}`).join('\n'));}
    else{toast(result?.message||'操作完成');if(name==='save')formLoaded=false;}
  }
}
function schedulePoll(delay=0){
  clearTimeout(pollTimer);
  if(document.hidden||hostHidden)return;
  if(polling){pollAgain=true;return;}
  pollTimer=setTimeout(poll,delay);
}
async function poll(){
  if(document.hidden||hostHidden||polling)return;
  polling=true;
  try{const selected=$('day').value;const params=new URLSearchParams({view:activePage,plane});if(selected)params.set('day',selected);const response=await fetch('/api/state?'+params,{signal:AbortSignal.timeout(8000)});if(!response.ok)throw Error('无法读取本机工具');const update=await response.json();snapshot={...snapshot,...update,state:{...snapshot?.state,...update.state}};if(!document.hidden&&!hostHidden)render();}
  catch(e){$('online').textContent='工具连接已断开';$('online').className='pill muted';}
  finally{polling=false;const delay=pollAgain?0:job?500:['overview','traffic'].includes(activePage)?2000:5000;pollAgain=false;schedulePoll(delay);}
}
document.addEventListener('visibilitychange',()=>schedulePoll(0));
if(window.chrome?.webview)window.chrome.webview.addEventListener('message',e=>{if(e.data?.type==='visibility'){hostHidden=!!e.data.hidden;schedulePoll(0);}});
document.querySelectorAll('[data-page]').forEach(x=>x.addEventListener('click',()=>page(x.dataset.page)));
document.querySelectorAll('[data-goto]').forEach(x=>x.addEventListener('click',()=>page(x.dataset.goto)));
document.querySelectorAll('[data-plane]').forEach(x=>x.addEventListener('click',()=>{plane=x.dataset.plane;document.querySelectorAll('[data-plane]').forEach(b=>b.classList.toggle('active',b===x));renderTable();schedulePoll(0);}));
for(const name of ['start','stop','import','preview','probe','recover','exit','warp-register','deps-install'])$(name).addEventListener('click',()=>action(name));
$('capture').addEventListener('click',()=>action(snapshot.capture.running||snapshot.state.capture_requested?'capture-stop':'capture-start'));
for(const role of ['ai','default'])$('node-'+role).addEventListener('change',()=>{nodeDirty=true;renderNodes();});
$('nodes-apply').addEventListener('click',()=>action('nodes-select',{ai_node:$('node-ai').value,default_node:$('node-default').value}));
$('nodes-probe').addEventListener('click',()=>action('nodes-probe'));
$('settings-form').addEventListener('submit',e=>{e.preventDefault();action('save',formValues());});
$('add-exception').addEventListener('click',()=>{const value=$('exception-value').value.trim();if(!value)return toast('请填写域名或进程名');exceptions.push({kind:$('exception-kind').value,value,route:$('exception-route').value});$('exception-value').value='';renderExceptions();toast('例外已加入草稿，点击“保存配置”生效。');});
$('exceptions').addEventListener('click',e=>{const b=e.target.closest('[data-remove]');if(b){exceptions.splice(Number(b.dataset.remove),1);renderExceptions();}});
$('search').addEventListener('input',renderTable);$('category').addEventListener('change',renderTable);
$('day').addEventListener('change',()=>{if(snapshot)$('export').href='/api/export?day='+encodeURIComponent($('day').value);schedulePoll(0);});
window.addEventListener('resize',chart);
// 允许直接用 ?page=traffic 这类链接打开某一页，便于收藏与排障。
const params=new URLSearchParams(location.search);
const deepPlane=params.get('plane');
if(deepPlane){plane=deepPlane;document.querySelectorAll('[data-plane]').forEach(b=>b.classList.toggle('active',b.dataset.plane===deepPlane));}
const deepLink=params.get('page');
if(deepLink&&document.getElementById(deepLink))page(deepLink);else schedulePoll(0);
// 每次打开都从页首开始，避免 WebView2 复用上次的滚动位置。
if('scrollRestoration' in history)history.scrollRestoration='manual';
const mainEl=document.querySelector('main');
const toTop=()=>{if(mainEl)mainEl.scrollTop=0;window.scrollTo(0,0);};
toTop();window.addEventListener('load',()=>{toTop();setTimeout(toTop,60)});
