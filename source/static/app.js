'use strict';
// No client-side fabricated quotes/signals. ALL values originate from backend responses.
const $ = id => document.getElementById(id);
const state = {page:'overview',rows:[],pageSize:120,visible:120,total:0,health:null,model:null,selected:'BTCUSDT',period:'15m',detailNonce:0};
// Server epoch + monotonic elapsed time: device clock/timezone cannot age quotes.
let serverClock=null;
function syncServerClock(ms){if(Number.isFinite(ms)&&ms>0){const now=performance.now();if(!serverClock||ms>=serverClock.ms+(now-serverClock.at))serverClock={ms,at:now}}}
function quoteNow(){return serverClock?(serverClock.ms+performance.now()-serverClock.at)/1000:null}
function quoteStamp(value){if(value===null||value===undefined)return NaN;return typeof value==='number'?value:Date.parse(value)/1000}
function quoteLabel(row){
  const now=quoteNow(),stamp=quoteStamp(row?.quote_updated_at);
  if(!Number.isFinite(stamp)||!row?.price)return 'Fiyat bekleniyor';
  if(now===null)return 'Saat doğrulanıyor';
  const age=now-stamp;
  if(age<0)return 'Zaman damgası ileride';
  return (age<10?'Güncel · ':'Gecikmiş · ')+n(Math.floor(age),0)+' sn';
}
function paintQuoteCell(cell,row){cell.replaceChildren(document.createTextNode(price(row.price)),elem('div',{className:'small muted'},quoteLabel(row)));cell.title='Son borsa olayı: '+(row.quote_updated_at?dt(quoteStamp(row.quote_updated_at)*1000):'—')}
function refreshQuoteAges(){
  const rows=new Map(state.rows.map(r=>[r.symbol,r]));
  for(const id of ['overviewRows','signalsRows'])for(const tr of $(id)?.rows||[]){const row=rows.get(tr.cells[0]?.textContent.trim());if(row&&tr.cells.length>=4)paintQuoteCell(tr.cells[1],row)}
  if(state.page==='details'){const row=rows.get(state.selected);setText('selectedLivePrice','Son fiyat: '+price(row?.price)+' · '+quoteLabel(row))}
}
const pageTitles={overview:'Genel Bakış',signals:'AI Sinyaller',details:'Grafik ve Detay',performance:'Geçmiş Performans',paper:'Sanal Sinyal Takibi',model:'Model Durumu',quality:'Kalite Raporu',settings:'Ayarlar ve Risk'};
const trTime = new Intl.DateTimeFormat('tr-TR',{timeZone:'Europe/Istanbul',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'});
function dt(v){try{return v?trTime.format(new Date(v)): '—'}catch{return '—'}}
function ts(v){return v?dt(v):'—'}
function n(v,d=2){return v===null||v===undefined||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('tr-TR',{maximumFractionDigits:d})}
function price(v){if(v===null||v===undefined)return '—'; const num=Number(v);return n(num,num>=100?2:num>=1?4:num>=.01?6:8)}
function pct(v){return v===null||v===undefined?'—':(Number(v)>=0?'+':'')+n(v,2)+'%'}
function prob(v){return v===null||v===undefined?'—':n(v*100,1)+'%'}
function abbr(v){if(v===null||v===undefined)return '—';let t=Number(v);return t>=1e9?n(t/1e9,2)+' Mr':t>=1e6?n(t/1e6,1)+' Mn':t>=1e3?n(t/1e3,1)+' B':n(t,0)}
function setText(id,value){let el=$(id);if(el)el.textContent=value}
function elem(tag,props={},text=''){let x=document.createElement(tag);for(let [k,v] of Object.entries(props)){if(k==='className')x.className=v;else x.setAttribute(k,v)}x.textContent=text;return x}
async function api(url,opts){let res=await fetch(url,{cache:'no-store',...opts});if(!res.ok){let body;try{body=await res.json()}catch{}throw new Error(body?.detail||`HTTP ${res.status}`)}return res.json()}
function signalClass(s){return s==='LONG'?'long':s==='SHORT'?'short':s==='BEKLE'?'wait':'unknown'}
function signalName(s){return s==='LONG'?'LONG':s==='SHORT'?'SHORT':s==='BEKLE'?'BEKLE':'SİNYAL YOK'}
function signalTag(s){let x=elem('span',{className:'tag '+signalClass(s)},signalName(s));return x}
function changeClass(e,cls,enable){e&&e.classList.toggle(cls,enable)}
function navigate(page){state.page=page;document.querySelectorAll('.page').forEach(x=>x.classList.toggle('visible',x.id===page));document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x.dataset.page===page));setText('pageName',pageTitles[page]);setText('heading',page==='overview'?'Futures AI Kontrol Merkezi':pageTitles[page]);if(page==='details')loadDetail();if(page==='signals')loadMarkets();if(page==='paper')loadPaper();if(page==='quality')loadQuality();window.scrollTo({top:0,behavior:'smooth'})}
function setError(message){let el=$('siteError');el.textContent=message||'';el.classList.toggle('hidden',!message)}
function setChip(id,ready,text){let el=$(id);if(!el)return;el.className='chip '+(ready?'green':'amber');el.textContent=text}
function paintHealth(h){state.health=h;let ready=h.provider_ok&&h.model_ready;
  syncServerClock(h.server_time);
  const monitor=h.execution_monitor||{};
  setText('bookCoverage',`${monitor.fresh_books||0} / ${monitor.total_symbols||0}`);
  setText('entryCandidates',String(monitor.active_candidates||0));setText('entryPassed',String(monitor.entry_passed||0));
  setText('bookConnection',`${monitor.book_stream||'Bekleniyor'} · 20 kademe: ${monitor.depth_stream||'Bekleniyor'} (${monitor.fresh_depth_books||0}/${monitor.depth_watched||0})`);
  setChip('executionChip',monitor.fresh_books>0,monitor.fresh_books>0?'GİRİŞ POLİTİKASI AKTİF':'VERİ BEKLENİYOR');
  $('executionReasons').replaceChildren(...(monitor.veto_reasons||[]).map(x=>elem('p',{},`${x.count} aday · ${x.reason}`)));
  if(!(monitor.veto_reasons||[]).length)setText('executionReasons','Engellenen güncel model adayı yok. Sinyal üretmek için doğrulanmış model ve yeni kapanmış mum gerekir.');
  setText('statAll',h.symbol_count?String(h.symbol_count):'—');const actual=h.signal_decisions_ready||0;
  setText('statCoverage',h.symbol_count?`${Math.round(actual/h.symbol_count*100)}%`:'—');
  $('coverageProgress').style.width=(h.symbol_count?Math.min(100,actual/h.symbol_count*100):0)+'%';setText('coverageText',`${actual} / ${h.symbol_count}`);
  const coins=h.coin_training||{};
  setText('heroStatus',h.binance_cooldown_seconds>0?'Binance REST beklemede':!h.provider_ok?'Veri bağlantısı yok':coins.current_symbol?'Coin eğitiliyor: '+coins.current_symbol:h.model_ready?'Coin bazlı AI aktif':'Coin modelleri hazırlanıyor');
  setText('heroStatusIcon',!h.provider_ok?'⚠':h.model_ready?'✦':'◌');
  setChip('modelChip',h.model_ready,'OY MODELİ: '+(coins.ensemble_trained||0)+'/'+(coins.eligible||h.symbol_count||0));
  setText('professionalCoverage',`Profesyonel dönem denetimi tamamlanan: ${coins.professional_trained||0}/${coins.eligible||h.symbol_count||0} · Geçiş bekleyen: ${coins.professional_pending||0} · Geçmiş hedefi ${n(coins.history_target||18000,0)} gerçek 15dk mum. Eğitim sırayla sürer; dönem testi geçmeden yön onaylanmaz.`);
  setText('ensembleCoverage',`Oylama eğitimi tamamlanan: ${coins.ensemble_trained||0}/${coins.eligible||h.symbol_count||0} coin · Geçiş bekleyen: ${coins.ensemble_pending||0} · Her yönde 3 algoritma hedefi, en az 2 oy modeli. Uzlaşma ve bağımsız yön testi olmadan işlem sinyali verilmez.`);
  const cause=h.signal_diagnostics?.model_causes||{};
  if(cause.directions_checked)$('ensembleCoverage').append(document.createTextNode(` ${coins.training_pipeline_version||'Güncel'} hesaplama geçişi: ${coins.pipeline_trained||0}/${coins.eligible||h.symbol_count||0}. Yön bazında: yalnız baz oranına düşen ${cause.baseline_only||0}, doğrulamada hiç aday üretmeyen ${cause.validation_no_candidates||0}, aday üretip pozitif stres sonucu bulunmayan ${cause.validation_no_positive_net||0}. Bunlar birbirleriyle örtüşebilir; canlı giriş vetolarından ayrı model sorunlarıdır.`));
  setText('statLong',String(h.unified_counts?.islem_sinyali??'—'));
  setText('statShort',String(h.unified_counts?.takip_et??'—'));
  setText('statTechLong',String(h.unified_counts?.long??'—'));
  setText('statTechShort',String(h.unified_counts?.short??'—'));
  setText('homeTechnical',`Birleşik sistem: ${h.unified_counts?.islem_sinyali||0} işlem onaylı · ${h.unified_counts?.takip_et||0} takip · ${h.unified_counts?.bekle||0} bekle`);
  setText('homeTraining',`7/24 oylama eğitimi: ${coins.ensemble_trained||0}/${coins.eligible||h.symbol_count||0} · LONG testli: ${coins.approved_long||0} · SHORT testli: ${coins.approved_short||0}`);
  setText('homeFresh',h.provider_ok?`${h.quote_coverage?.fresh||0}/${h.symbol_count||0} fiyat 10 sn'den yeni · ${h.quote_coverage?.stale||0} gecikmiş`:'Veri güncel değil');
  setText('updateTime',h.data_updated_at?(h.provider_ok?'Veri: ':'Eski veri: ')+dt(h.data_updated_at):'Güncelleme bekleniyor');
  setText('storageNotice',h.storage_persistent_configured?'Kalıcı disk ayarı bildirildi':'Kalıcı disk doğrulanmadı; yeniden başlatmada veriler kaybolabilir');
  setText('liveKlineState',h.kline_websocket||'Bekleniyor');
  setText('liveWatchCount',`${h.live_watch_count||0} / ${h.symbol_count||0} (hedef ${h.watch_target_count||0})`);
  setText('warmupProgress',`Geçmiş mum hazır: ${h.daily_history_ready||0}/${h.watch_target_count||0} · Oylama modeli hazır: ${coins.ensemble_trained||0}/${coins.eligible||h.symbol_count||0} · Güncel AI kararları: ${h.signal_decisions_ready||0}/${h.symbol_count||0}`);
  setText('lastClosedBar',h.last_closed_kline_at?'Son kapanmış mum: '+dt(h.last_closed_kline_at):'Yeni mum kapanışı bekleniyor');
  $('sideConnection').replaceChildren(elem('span',{className:'dot '+(h.provider_ok?'green':'red')}),document.createTextNode(h.provider_ok?'Binance bağlantısı aktif':'Binance bağlantısı yok'));
  setError(h.binance_cooldown_seconds>0?'Binance REST engeli: '+h.binance_cooldown_seconds+' sn. REST taraması durdu; WebSocket kapanmış mumları birikiyor. Kesintisiz güncel mum geçmişi olmayan coinlerde sinyal verilmez.':!h.provider_ok?(h.last_error?'Binance veri bağlantısı: '+h.last_error:'Binance fiyatları alınamıyor. Veriler ve sinyaller canlı olarak sunulamaz.'):'');
}
async function loadHealth(){try{paintHealth(await api('/api/health'))}catch(e){setError('Sunucuya bağlanılamıyor: '+e.message)}}
function decisionBadge(u){
  if(!u||!u.direction)return signalTag('BEKLE');
  const approved=u.status==='İŞLEM SİNYALİ';
  return elem('span',{className:'tag '+(approved?signalClass(u.direction):'wait')},
              u.direction+(approved?' · ONAYLI':' · TAKİP'));
}
function decisionExplanation(row){
  const u=row.unified||{},a=u.execution_quality||{},failed=new Set((a.checks||[]).filter(c=>!c.passed).map(c=>c.id));
  const lines=[u.reason||'Karar bekleniyor'];
  if(failed.has('liquidity'))lines.push(`${a.liquidity_method==='depth20_vwap'?'20 kademe':'Üst kademe'} görünür tutar: ${n(a.min_top_notional_usdt,2)} / gereken ${n(a.assumptions?.hypothetical_notional_usdt,0)} USDT`);
  if(failed.has('departure'))lines.push(`Son fiyat uzaklığı: ${n(a.reference_departure_atr,3)} / sınır ${n(a.reference_departure_limit_atr,3)} ATR`);
  if(failed.has('executable'))lines.push(`Giriş fiyatı uzaklığı: ${n(a.executable_departure_atr,3)} / sınır ${n(a.reference_departure_limit_atr,3)} ATR`);
  if(failed.has('depth_impact'))lines.push(`Kademe kayması: ${n(a.depth_impact_bps,2)} / sınır ${n(a.assumptions?.max_depth_impact_bps,2)} baz puan`);
  if(failed.has('quote'))lines.push(`Fiyat yaşı: ${n(a.quote_quality?.age_seconds,1)} saniye`);
  if(u.status==='TAKİP ET'&&failed.size)lines.push('Giriş kontrolleri henüz geçilmedi; takip kararı işlem onayı değildir.');
  return lines.join('\n');
}
function rowCells(row,details=false){
  let t=elem('tr');let sym=elem('td');sym.append(elem('span',{},row.symbol));t.append(sym);
  const priceCell=elem('td');paintQuoteCell(priceCell,row);t.append(priceCell);
  t.append(elem('td',{className:row.change_pct>=0?'positive':'negative'},pct(row.change_pct)));
  t.append(elem('td',{},abbr(row.quote_volume)));
  const u=row.unified||{};let c=elem('td');c.append(decisionBadge(u));t.append(c);
  t.append(elem('td',{},u.score==null?'—':n(u.score,0)+'/100'));
  if(details){
    const reasons=elem('td',{className:'small score-explanation'});
    const parts=u.components||{};
    const text=(u.score==null?'Puan hesaplanmadı':'AI '+n(parts.ai,1)+' · Gösterge '+n(parts.indicators,1)+' · Trend '+n(parts.higher_timeframe,1)+' · Formasyon '+n(parts.patterns,1)+' · Geçmiş teyit '+n(parts.tested_setup,1)+' · Risk kesintisi −'+n(parts.risk_penalty,1));
    reasons.textContent=text;
    t.append(reasons);
    t.append(elem('td',{className:'small decision-reason'},decisionExplanation(row)));
    t.append(elem('td',{className:'muted'},dt(row.candle_closed_at)));
    t.append(elem('td',{className:'muted'},row.coverage));
    const act=elem('td'),btn=elem('button',{className:'details-button','aria-label':row.symbol+' grafiğini aç'},'İncele →');
    btn.addEventListener('click',()=>{state.selected=row.symbol;navigate('details')});act.append(btn);t.append(act);
  }else{t.style.cursor='pointer';t.title='Grafiği aç';t.addEventListener('click',()=>{state.selected=row.symbol;navigate('details')})}
  return t;
}
function paintMarkets(data){
  const previous=new Map(state.rows.map(r=>[r.symbol,r]));
  state.rows=(data.rows||[]).map(row=>{const old=previous.get(row.symbol);return old&&quoteStamp(old.quote_updated_at)>quoteStamp(row.quote_updated_at)?{...row,price:old.price,change_pct:old.change_pct,quote_volume:old.quote_volume,quote_updated_at:old.quote_updated_at}:row});state.total=data.total||0;state.visible=state.pageSize;
  syncServerClock(data.server_time);
  let home=$('overviewRows');home.replaceChildren();if(!state.rows.length){let tr=elem('tr');tr.append(elem('td',{colspan:'6',className:'placeholder'},'Uygun piyasa kaydı yok. Veri kaynağını kontrol edin.'));home.append(tr)}
  [...state.rows].sort((a,b)=>(b.unified?.status==='İŞLEM SİNYALİ'?2:b.unified?.status==='TAKİP ET'?1:0)-(a.unified?.status==='İŞLEM SİNYALİ'?2:a.unified?.status==='TAKİP ET'?1:0)||(b.unified?.score||0)-(a.unified?.score||0)).slice(0,9).forEach(r=>home.append(rowCells(r)));
  fillSymbolPicker();paintSignals();
}
function fillSymbolPicker(){const picker=$('symbolSelect');let old=state.selected;let current=Array.from(picker.options).map(o=>o.value);
  let options=state.rows.map(x=>x.symbol);
  if(options.length===current.length&&options[0]===current[0]&&options[options.length-1]===current[current.length-1])return;
  picker.replaceChildren();for(let sym of options){let option=elem('option',{value:sym},sym);picker.append(option)}
  if(options.includes(old))picker.value=old;else if(options.length){state.selected=options[0];picker.value=state.selected}
}
function paintSignals(){let tbody=$('signalsRows');tbody.replaceChildren();let slice=state.rows.slice(0,state.visible);
  slice.forEach(row=>tbody.append(rowCells(row,true)));
  if(!slice.length){let tr=elem('tr');tr.append(elem('td',{colspan:'11',className:'placeholder'},'Eşleşen sözleşme yok veya veri bekleniyor.'));tbody.append(tr)}
  setText('resultCount',`${state.total} sonuç`);setText('paginationText',`${slice.length} / ${state.total} gösteriliyor`);$('loadMore').classList.toggle('hidden',state.visible>=state.rows.length);
}
// UI price transport: same-origin WebSocket, fed only by authenticated Binance
// server-side quote events. Signals remain on the independent 15m candle schedule.
let quoteSocket=null;
let reconnectTimer=null;
let reconnectMs=1500;
let lastQuoteMessage=0;
let fallbackInFlight=false;

function markQuoteConnection(text){setText('quoteStreamStatus',text)}
function applyQuoteChanges(quotes){
  if(!quotes||typeof quotes!=='object')return;
  const bySymbol=new Map(state.rows.map(row=>[row.symbol,row]));
  const changed=new Map(),now=quoteNow();
  if(now===null)return;
  for(const [symbol,data] of Object.entries(quotes)){
    if(!Array.isArray(data)||data.length<4)continue;
    const row=bySymbol.get(symbol);
    if(!row)continue;
    const [p,ch,v,t]=data.map(Number);
    if(![p,ch,v,t].every(Number.isFinite)||p<=0||v<0||now-t<0||now-t>=120)continue;
    if(quoteStamp(row.quote_updated_at)>t)continue;
    row.quote_updated_at=t;
    row.price=p;row.change_pct=ch;row.quote_volume=v;
    changed.set(symbol,row);
  }
  if(!changed.size)return;
  for(const id of ['overviewRows','signalsRows']){
    const tbody=$(id);if(!tbody)continue;
    for(const tr of tbody.rows){
      if(tr.cells.length<4)continue;
      const sym=tr.cells[0].textContent.trim(),r=changed.get(sym);
      if(!r)continue;
      paintQuoteCell(tr.cells[1],r);
      tr.cells[2].textContent=pct(r.change_pct);
      tr.cells[2].className=r.change_pct>=0?'positive':'negative';
      tr.cells[3].textContent=abbr(r.quote_volume);
    }
  }
  const chosen=changed.get(state.selected);
  if(chosen&&state.page==='details')setText('selectedLivePrice','Son fiyat: '+price(chosen.price)+' · '+quoteLabel(chosen));
  markQuoteConnection('Fiyat akışı bağlı · güncellik coin bazında gösterilir');
}
function connectPriceStream(){
  if(document.hidden||quoteSocket&&quoteSocket.readyState<=WebSocket.OPEN)return;
  clearTimeout(reconnectTimer);
  const url=(location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws/prices';
  let socket;
  try{socket=new WebSocket(url)}catch{schedulePriceReconnect();return}
  quoteSocket=socket;
  socket.onopen=()=>{reconnectMs=1500;lastQuoteMessage=Date.now();markQuoteConnection('Fiyat akışı bağlandı')};
  socket.onmessage=event=>{
    let msg;try{msg=JSON.parse(event.data)}catch{return}
    syncServerClock(msg.server_time);
    lastQuoteMessage=Date.now();
    if(msg.type==='quotes')applyQuoteChanges(msg.quotes);
    else if(msg.type==='heartbeat'&&msg.source!=='WebSocket: Bağlandı')markQuoteConnection('Piyasa bağlantısı bekleniyor');
  };
  socket.onerror=()=>{};
  socket.onclose=()=>{if(quoteSocket===socket)quoteSocket=null;if(!document.hidden)schedulePriceReconnect()};
}
function schedulePriceReconnect(){
  if(document.hidden)return;
  clearTimeout(reconnectTimer);
  reconnectTimer=setTimeout(connectPriceStream,reconnectMs);
  reconnectMs=Math.min(30000,Math.round(reconnectMs*1.8));
  markQuoteConnection('Canlı bağlantı yeniden deneniyor');
}
async function lightPriceFallback(){
  if(document.hidden||fallbackInFlight)return;
  if(quoteSocket?.readyState===WebSocket.OPEN&&Date.now()-lastQuoteMessage<15000)return;
  fallbackInFlight=true;
  try{const data=await api('/api/prices');syncServerClock(data.server_time);if(data.type==='quotes')applyQuoteChanges(data.quotes)}
  catch{markQuoteConnection('Güncel fiyat bağlantısı bekleniyor')}
  finally{fallbackInFlight=false}
}
document.addEventListener('visibilitychange',()=>{
  if(document.hidden){clearTimeout(reconnectTimer);if(quoteSocket)quoteSocket.close()}
  else{connectPriceStream();refreshAll()}
});
setInterval(lightPriceFallback,5000);
setInterval(()=>{if(!document.hidden)refreshQuoteAges()},1000);
let searchTimer=null;
async function loadOpportunities(){
  const panel=$('quickPlanRows');if(!panel)return;
  try{
    const data=await api('/api/opportunities?limit=12');panel.replaceChildren();
    const counts=data.counts||{};
    setText('quickPlanCount',`${counts.islem_sinyali||0} onaylı işlem sinyali · ${counts.takip_et||0} birleşik takip fırsatı · ${counts.bekle||0} bekle`);
    const shown=data.rows||[];
    if(!shown.length){panel.append(elem('p',{className:'placeholder'},'Şu anda onaylı işlem sinyali veya teknik fırsat yok. Son 15 dakikalık mum ve model eğitimini bekleyin.'));return}
    for(const item of shown){
      const approved=item.kind==='İŞLEM SİNYALİ';
      const card=elem('article',{className:'v60-item'});
      const header=elem('header');header.append(elem('strong',{},item.symbol),elem('span',{className:'v60-kind '+(approved?(item.direction==='LONG'?'green':'red'):'ambertext')},item.direction+' · '+item.kind));card.append(header);
      const prices=elem('div',{className:'v60-prices'});
      for(const [label,value] of [['Giriş (referans)',item.entry_reference],['TP (hedef)',item.tp_estimate],['SL (risk)',item.sl_estimate]]){
        const cell=elem('div');cell.append(elem('small',{},label),elem('b',{},price(value)));prices.append(cell)}
      card.append(prices);
      const u=item.unified||{};
      const text='BİRLEŞİK PUAN: '+n(u.score,0)+'/100 (TP olasılığı değil)'+(approved?' · AI test onaylı':' · AI işlem onayı yok');
      const history=(item.historical_resolved||0)>0?' · Geçmişte '+item.historical_resolved+' sonuç, TP '+prob(item.historical_tp_rate)+' · masraf sonrası ortalama '+(item.historical_stress_net==null?'—':pct(item.historical_stress_net*100)):' · Geçmiş doğrulama yetersiz';
      card.append(elem('div',{className:'v60-note'},text+history));
      card.append(elem('div',{className:'v60-note'},'Puan dağılımı: AI '+n(u.components?.ai,1)+' · Göstergeler '+n(u.components?.indicators,1)+' · 1s/4s '+n(u.components?.higher_timeframe,1)+' · Formasyon '+n(u.components?.patterns,1)+' · Geçmiş teyit '+n(u.components?.tested_setup,1)+' · Risk cezası -'+n(u.components?.risk_penalty,1)));
      card.append(elem('div',{className:'v60-note'},'Giriş değerlendirmesi: '+(item.entry_timing?.status||'takip bekleniyor')+' · '+(item.entry_timing?.why||'')));
      card.append(elem('div',{className:'v60-note'},approved?'AI modeli bağımsız doğrulama koşullarını geçti; kâr garantisi yok.':'Tek birleşik LONG/SHORT eğilimi: takip amaçlıdır; doğrulanmış işlem sinyali değildir.'));
      const btn=elem('button',{className:'details-button'},'Grafik ve gerekçe →');btn.addEventListener('click',()=>{state.selected=item.symbol;navigate('details')});card.append(btn);
      panel.append(card)
    }
  }catch(e){setText('quickPlanCount','Fırsat listesi alınamadı: '+e.message)}
}
async function loadMarkets(){try{let params=new URLSearchParams({search:$('search').value.trim(),signal:$('signalFilter').value,sort:$('sortBy').value,limit:'2500'});let m=await api('/api/markets?'+params);paintMarkets(m);if(state.page==='overview'&&!m.fresh)setText('homeFresh','Veri güncel değil')}catch(e){setError('Piyasa listesi alınamadı: '+e.message)}}
async function loadModel(){try{let obj=await api('/api/model');state.model=obj;
  if(obj.scope==='independent_model_per_symbol'){
    let x=obj.coin_training||{},total=x.eligible||0,trained=x.professional_trained||0;
    setText('homeVersion',`${trained}/${total} profesyonel model`);
    setChip('modelTag',trained>0,`${trained}/${total} PROFESYONEL`);
    setText('modelAlgo','Logistic + Gradient Boosting + Extra Trees · ağırlıklı oy');
    const migration=`${x.training_pipeline_version||'Güncel'} geçişi: ${x.pipeline_trained||0}/${total} · Bekleyen: ${x.pipeline_pending??total}`;
    setText('modelVersion',migration);
    setText('modelDate','Her coin için ayrı tarih');
    setText('modelDates','Hedef 18.000 gerçek 15dk mum (yaklaşık 187 gün); yeni coinler daha kısa olabilir');
    setText('trainSize','Coin bazında');setText('valSize','Kronolojik ayrım');
    setText('testSize','Coin bazında');setText('trainSymbolCount',String(trained));
    setText('modelStage',obj.stage);
    setText('perfAccuracy','Coin bazında');setText('perfF1','Coin bazında');
    setText('perfTrades','Onaylı yönler');setText('perfDirectional','—');
    setText('performanceCaution',`Her coin ayrı TP/SL oylama modeli: ${trained}/${total} eğitildi. Üç algoritma hedeflenir; en az iki model birlikte oy verir. Ağırlıklar ve kalibrasyon doğrulama döneminden belirlenir. Yeterli uzlaşma yoksa BEKLE. Bağımsız TP/SL doğrulaması geçen LONG: ${x.approved_long||0}, SHORT: ${x.approved_short||0}. En az 9.000 gerçek mum, 24 saat ayrılmış 12 sonuç, üç dönem tutarlılığı ve 1.400 yön testi için çoklu seçim denetimi gerekir. Yeterli kanıtı olmayan yönler BEKLE olur. Bunlar gerçek kâr garantisi değildir.`);
    setText('perfReturn','—');setText('perfCost','%0,16 varsayım');
    let pendingInfo = state.health?.coin_training?.pending_symbols||[];
    let failureInfo = state.health?.coin_training?.pending_reasons||{};
    let issueText = pendingInfo.length?` Bekleyenler: ${pendingInfo.join(', ')}. ${Object.entries(failureInfo).map(([s,r])=>s+': '+r).join('; ')}`:'';
    setText('coinTrainingDetails',`${migration}. Geçiş bekleyenler işlem hacmine göre sırayla eğitilir. Coin başına ${state.health?.coin_training?.refresh_interval_hours||6} saat aralıklı yenileme. ${trained}/${total} profesyonel model. Detaylar: /api/coin-models.${issueText}`);
    const diag=state.health?.signal_diagnostics;
    if(diag){
      const reasons=Object.entries(diag.model_rejections||{}).flatMap(([side,rows])=>rows.slice(0,3).map(row=>`${side}: ${row.reason} (${row.count} model)`));
      const stages=diag.stages||{};
      setText('coinTrainingDetails',`${migration}. ${trained}/${total} profesyonel model · Canlı hesaplama için ${diag.required_inference_bars} kapanmış mum. Veri bekleyen: ${stages.history_pending||0} · Eşik/yön teyidi bekleyen: ${stages.score_or_direction_margin||0} · Giriş onayı: ${stages.entry_approved||0}. ${reasons.join(' · ')}. Ret koşulları aynı modelde birlikte bulunabilir. Ayrıntı: /api/signal-diagnostics.${issueText}`);
    }
    return;
  }
  let info=obj.info,mt=info?.metrics;
  setText('homeVersion',info?.version||'Eğitim bekleniyor');setChip('modelTag',obj.ready,obj.ready?'EĞİTİLDİ':'BEKLENİYOR');
  setText('modelAlgo',info?.algorithm||'—');setText('modelVersion',info?.version||'—');setText('modelDate',dt(info?.trained_at));
  setText('modelDates',info?dt(info.data_start)+' — '+dt(info.data_end):'—');setText('trainSize',info?n(info.training_sample_count,0):'—');
  setText('valSize',info?n(info.validation_sample_count,0):'—');setText('testSize',info?n(info.test_sample_count,0):'—');
  setText('trainSymbolCount',info?.training_symbols?.length?String(info.training_symbols.length):'—');setText('modelStage',obj.stage);
  setText('perfAccuracy',mt?pct(mt.test_accuracy*100):'—');setText('perfF1',mt?n(mt.test_macro_f1,3):'—');
  setText('perfTrades',mt?n(mt.test_signals,0):'—');setText('perfDirectional',mt&&mt.test_signal_direction_accuracy!==null?pct(mt.test_signal_direction_accuracy*100):'—');
  setText('performanceCaution',obj.directional_signal_permissions?(Object.entries(obj.directional_signal_permissions).map(([k,v])=>k+': '+(v.approved?'test şartları geçti':'YÖN SİNYALİ KAPALI')).join(' · ')+'. Yön testi yetersizken hiçbir LONG/SHORT onaylanmaz.'):'Model testi bekleniyor');
  setText('perfReturn',mt&&mt.test_mean_net_return_per_signal!==null?pct(mt.test_mean_net_return_per_signal*100):'—');setText('perfCost',mt?pct(mt.assumed_roundtrip_fee_slippage*100):'—');
}catch(e){setError('Model bilgisi alınamadı: '+e.message)}}
function fillProbability(idBar,idText,v){setText(idText,prob(v));$(idBar).style.width=(v==null?0:Math.round(v*100))+'%'}
function paintPrediction(pred,unified){
  const u=unified||{};
  setText('predictionVersion',pred?'Model: '+pred.model_version+' · AI ve teknik teyit birlikte':'Henüz geçerli eğitimli AI tahmini yok');
  let label=$('predSignal');const side=u.direction||'BEKLE';
  label.textContent=pred?(side+(u.status==='TAKİP ET'?' · TAKİP ET':u.status==='İŞLEM SİNYALİ'?' · ONAYLI':'')):'Sinyal henüz yok';
  label.className='pred-signal '+(u.status==='İŞLEM SİNYALİ'?(side==='LONG'?'green':'red'):'ambertext');
  setText('pLong',u.long_score==null?'—':n(u.long_score,0)+'/100');setText('pShort',u.short_score==null?'—':n(u.short_score,0)+'/100');setText('pWait',u.status||'BEKLE');
  $('barLong').style.width=(u.long_score||0)+'%';$('barShort').style.width=(u.short_score||0)+'%';$('barWait').style.width=u.status==='BEKLE'?'100%':'0%';
  const c=u.components||{},tech=pred?.technical||{}, audit=pred?.execution_quality;
  $('entryChecks').replaceChildren(...(audit?.checks||[]).map(c=>elem('span',{className:'chip '+(c.passed?'green':'amber')},`${({quote:'Fiyat',candle:'Mum',departure:'Giriş mesafesi',reward:'Maliyet',bracket:'TP/SL',book:'Alış/satış',spread:'Makas',liquidity:'Görünür tutar',depth_impact:'Kademe kayması',executable:'Giriş fiyatı',stress:'Stres',model:'Model testi',consensus:'Oy uzlaşması',regime:'Piyasa koşulu',drift:'Sonuç kilidi'})[c.id]||'Veri'} · ${c.passed?'GEÇTİ':'BEKLE'}`)));
  setText('entryAudit',audit?`Giriş denetimi: ${audit.approved?'GEÇTİ':'BEKLE'} · Makas ${n(audit.spread_bps,2)} bp · Emir defteri üst kademe ${n(audit.min_top_notional_usdt,0)} / gereken ${n(audit.assumptions?.min_top_notional_usdt,0)} USDT · Referans uzaklığı ${n(audit.reference_departure_atr,2)} / sınır ${n(audit.reference_departure_limit_atr,3)} ATR · Varsayılan maliyet sonrası ödül / risk ${n(audit.assumed_net_risk_reward,2)} · ${(audit.reasons||[]).join(' · ')}`:'Güncel giriş denetimi bekleniyor.');
  const votes=pred?.ensemble; const voteText=votes?['LONG','SHORT'].map(side=>{const d=votes[side];return side+': '+(d?d.members.map(m=>`${m.name} ${prob(m.tp_score)} (ağırlık ${prob(m.weight)})`).join(' · ')+` · Uzlaşma ${d.agreement?'VAR':'YOK'}`:'oylama modeli yok')}).join(' | '):'Çoklu model eğitimi bekleniyor';
  setText('modelVotes',voteText);
  setText('regimeDetails',pred?.market_condition?`Piyasa koşulu: ${pred.market_condition.label} · Bu koşulda bağımsız sonuç: ${pred.regime_evidence?.independent_resolved||0} · Koşul izni: ${pred.regime_evidence?.allowed?'VAR':'YOK'}`:'Piyasa koşulu / yeni dönem testi bekleniyor');
  setText('predTechnical',(u.reason||'Birleşik değerlendirme bekleniyor')+' · AI '+n(c.ai,1)+' + teknik '+n(c.indicators,1)+' + 1s/4s '+n(c.higher_timeframe,1)+' + formasyon '+n(c.patterns,1)+' + tarihsel teyit '+n(c.tested_setup,1)+' - stres cezası '+n(c.risk_penalty,1)+' puan. '+(u.matching_patterns||[]).join(', ')+' · Bu puan TP başarı olasılığı DEĞİLDİR.');
  setText('entry',price(u.entry_reference));setText('stop',price(u.sl_estimate));setText('target',price(u.tp_estimate));setText('candleDate',pred?dt(pred.candle_time):'—');
}
function paintHoldoutAudit(validation){
  const tbody=$('holdoutAuditRows');tbody.replaceChildren();
  for(const side of ['LONG','SHORT']){const d=validation?.[side]?.holdout_audit?.independent_24h,tr=elem('tr');
    tr.append(elem('td',{},side));
    if(d)tr.append(elem('td',{},`${d.resolved} / ${d.signals}`),elem('td',{},prob(d.tp_rate)),elem('td',{},prob(d.wilson_lower_95)),elem('td',{},d.mean_stressed_net==null?'—':pct(d.mean_stressed_net*100)));
    else tr.append(elem('td',{colspan:'4'},'Bir sonraki coin eğitiminde oluşacak'));
    tbody.append(tr);
  }
}
async function loadQuality(){
  try{const r=await api('/api/professional-report'),d=r.daily||{},c=r.calibration||{},m=r.models||{};
    setText('qualityDay',`${d.day||'—'} · Europe/Istanbul · Son ölçüm ${dt(d.as_of)}`);
    setText('qualityDecisions',n(d.decisions,0));setText('qualityApproved',`${d.approved_decisions||0} / ${d.blocked_candidates||0}`);
    setText('qualityExits',`${d.tp_today||0} / ${d.sl_today||0}`);setText('qualityFunding',n(r.funding?.pending_results||0,0));
    setText('qualityNet',d.mean_net_closed_today==null?'—':pct(d.mean_net_closed_today*100));setText('qualityLossRun',n(d.max_negative_sequence_today||0,0));
    $('qualityReasons').replaceChildren(elem('p',{},'Günlük geçmiş kayıtlarıdır; anlık bağlantı arızası veya güncel coin sayısı değildir.'),...(d.veto_reasons||[]).map(x=>elem('p',{},`${x.count} karar · ${x.reason}`)));
    if(!(d.veto_reasons||[]).length)setText('qualityReasons','Henüz engellenen yön adayı kaydı yok. BEKLE kararları ayrıca sayılır.');
    setText('qualityModels',`Profesyonel eğitim: ${m.professional_trained||0}/${m.eligible||0} · LONG onaylı: ${m.approved_long||0} · SHORT onaylı: ${m.approved_short||0} · Gerçek geçmiş hedefi ${n(m.history_target||18000,0)} mum · Funding veri hatası: ${r.funding?.last_error||'yok'}`);
    setText('qualityCalibration',`${c.status||'ÖRNEK AZ'} · 24s içinde çözülen: ${c.resolved_within_24h||0} · Çözülmeyen / geç kapanan: ${c.censored_24h||0} · Veri eksik: ${c.unverified||0} · Henüz 24s dolmayan: ${c.immature||0} · Brier ${n(c.brier,4)} · ECE ${n(c.ece,4)}`);
    $('calibrationRows').replaceChildren(...(c.bins||[]).map(b=>{const tr=elem('tr');tr.append(elem('td',{},`${prob(b.from)}–${prob(b.to)}`),elem('td',{},n(b.samples,0)),elem('td',{},prob(b.mean_score)),elem('td',{},prob(b.tp_rate)),elem('td',{},prob(b.wilson_lower_95)),elem('td',{},b.status));return tr}));
    if(!(c.bins||[]).length){const tr=elem('tr');tr.append(elem('td',{colspan:'6'},'Onaylı ileri gözlemler birikince oluşacak.'));$('calibrationRows').append(tr)}
    const groups=r.risk_groups||{};$('correlationGroups').replaceChildren(...(groups.groups||[]).map((g,i)=>elem('p',{},`Grup ${i+1}: `+g.map(x=>`${x.symbol} ${x.direction}`).join(' · '))));
    if(!(groups.groups||[]).length)setText('correlationGroups',`Şu anda doğrulanmış benzer maruziyet kümesi yok. İncelenen aday: ${groups.tested_symbols||0} · Veri yetersiz çift: ${groups.unknown_pairs||0}. Bu durum adayların bağımsızlığını kanıtlamaz.`);
    $('dailyQualityHistory').replaceChildren(...(r.daily_history||[]).map(x=>elem('p',{},`${x.day} · ${x.decisions} karar · ${x.new_approved_observations} onaylı gözlem · TP ${x.tp_today} / SL ${x.sl_today}`)));
  }catch(e){setText('qualityDay','Kalite raporu alınamadı: '+e.message)}
}
function paintHistory(items){let t=$('historyRows');t.replaceChildren();if(!items.length){let tr=elem('tr');tr.append(elem('td',{colspan:'5',className:'placeholder'},'Modelin kaydedilmiş sinyali henüz yok.'));t.append(tr);return}
 for(let i of items){let tr=elem('tr');tr.append(elem('td',{},dt(i.candle_time)),(()=>{let td=elem('td');td.append(signalTag(i.signal));return td})(),elem('td',{},prob(i.confidence)),elem('td',{},price(i.entry_reference)),elem('td',{className:'muted'},i.model_version));t.append(tr)}
}
async function loadDetail(){let nonce=++state.detailNonce;const s=state.selected,tf=state.period;setText('detailTitle',s);setText('chartCoin',s+' · '+tf);refreshQuoteAges();$('symbolSelect').value=s;setText('chartCaption','Gerçek Binance '+tf+' mum grafiği');
  try{let [detail,chart]=await Promise.all([api('/api/symbol/'+encodeURIComponent(s)),api('/api/candles/'+encodeURIComponent(s)+'?'+new URLSearchParams({interval:tf,limit:'100'}))]);if(nonce!==state.detailNonce)return;paintPrediction(detail.prediction,detail.unified);paintHoldoutAudit(detail.directional_holdout);paintHistory(detail.history||[]);setText('chartSource',chart.source||'Binance REST');renderChart(chart.candles||[])}catch(e){if(nonce!==state.detailNonce)return;$('chartContainer').replaceChildren(elem('div',{className:'placeholder'},'Gerçek grafik alınamıyor: '+e.message));paintPrediction(null);paintHoldoutAudit({});paintHistory([])}
}
// Static SVG candlestick renderer, uses only real OHLC series from the server.
function renderChart(candles){let holder=$('chartContainer');holder.replaceChildren();if(!candles||candles.length<10){holder.append(elem('div',{className:'placeholder'},'Yeterli mum verisi yok.'));return}
 const series=candles.slice(-90), w=1000,h=320,left=8,right=79,top=16,bottom=28,innerW=w-left-right,innerH=h-top-bottom;
 const values=series.flatMap(c=>[c.high,c.low]),mn=Math.min(...values),mx=Math.max(...values),pad=(mx-mn)*.07||mx*.001,lo=mn-pad,hi=mx+pad, y=v=>top+(hi-v)/(hi-lo)*innerH;
 const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox',`0 0 ${w} ${h}`);svg.setAttribute('role','img');svg.setAttribute('aria-label','Binance fiyat mum grafiği');svg.setAttribute('preserveAspectRatio','none');
 const add=(name,attrs,text)=>{let node=document.createElementNS(svg.namespaceURI,name);for(let [k,v] of Object.entries(attrs||{}))node.setAttribute(k,String(v));if(text!==undefined)node.textContent=text;svg.appendChild(node);return node};
 for(let i=0;i<=5;i++){let yy=top+i*innerH/5,pr=hi-i*(hi-lo)/5;add('line',{x1:left,y1:yy,x2:w-right,y2:yy,stroke:'#28384d','stroke-dasharray':'4,6'});add('text',{x:w-right+10,y:yy+4,fill:'#91a5c0','font-size':11},price(pr))}
 let step=innerW/series.length;
 series.forEach((c,i)=>{let cx=left+(i+.5)*step,fill=c.close>=c.open?'#35d7ab':'#fb7186';add('line',{x1:cx,y1:y(c.high),x2:cx,y2:y(c.low),stroke:fill,'stroke-width':Math.max(.9,step*.12)});let topBody=Math.min(y(c.open),y(c.close)),bh=Math.max(1.5,Math.abs(y(c.open)-y(c.close)));add('rect',{x:cx-Math.max(2,step*.33),y:topBody,width:Math.max(3,step*.66),height:bh,fill,rx:.5})});
 for(let i=0;i<6;i++){let index=Math.min(series.length-1,Math.floor(i*(series.length-1)/5));add('text',{x:left+(index+.5)*step,y:h-7,fill:'#8ca2c1','font-size':11,'text-anchor':i===0?'start':i===5?'end':'middle'},new Date(series[index].time).toLocaleString('tr-TR',{timeZone:'Europe/Istanbul',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}))}
 holder.append(svg)
}

async function loadPaper(){
  try{const response=await api('/api/paper?limit=200'), summary=response.summary||{},rows=response.rows||[];
    if($('siteError').textContent.startsWith('Sanal gözlemler alınamadı:'))setError('');
    const policy=response.current_entry_policy||'V7.2',v7=response.entry_policy_summary?.[policy]||{};
    setText('paperPolicyNote',`${policy} giriş politikasıyla ${v7.observed||0} onaylı gözlem, ${v7.closed||0} kapanmış sonuç. Genel toplamlar önceki giriş politikalarını da içerir; yeni politikanın sonuçları burada ayrı sayılır.`);
    setText('paperTotal',n(summary.observed,0));setText('paperClosed',n(summary.settled,0));
    setText('paperWin',summary.win_rate===null?'—':pct(summary.win_rate*100));
    setText('paperAverage',summary.mean_net_return===null?'—':pct(summary.mean_net_return*100));
    setText('paperOpen',n(summary.by_state?.OPEN||0,0));setText('paperGap',n(summary.by_state?.VERI_EKSIK||0,0));
    setText('paperLegacyNote',`Önceki yöntemlerden ${summary.legacy_observations_excluded||0} kayıt ayrı tutuldu. Funding bekleyen çıkış: ${summary.by_state?.AWAITING_FUNDING||0}. TP'ye ilk temas: ${summary.tp_count||0}, SL'ye ilk temas: ${summary.sl_count||0}. Doğrulanmamış yönler ayrı TEST AMAÇLI takip edilir: ${summary.shadow_observations||0} aday, ${summary.shadow_settled||0} sonuçlandı; TP: ${summary.shadow_tp_count||0}, SL: ${summary.shadow_sl_count||0}, doğrulanamayan: ${summary.shadow_unverified||0}. Test adayları onaylı sinyal istatistiğine DAHİL DEĞİL ve yatırım başarısını kanıtlamaz.`);
    const evidence=$('directionalRows');evidence.replaceChildren();
    for(const side of ['LONG','SHORT']){const d=response.directional?.[side]||{}, tr=elem('tr');
      const pf=d.profit_factor==null?(d.profit_factor_status==='no_losses'?'Zarar örneği yok':'—'):n(d.profit_factor,2);
      tr.append(elem('td',{},side),elem('td',{},`${d.settled||0} / ${d.observed||0}`),elem('td',{},prob(d.tp_first_rate)),elem('td',{},prob(d.tp_wilson_lower_95)),elem('td',{},pf),elem('td',{},d.mean_net_return==null?'—':pct(d.mean_net_return*100)),elem('td',{},n(d.unverified||0,0)),elem('td',{},d.evidence_status||'ÖRNEK AZ'));
      evidence.append(tr);
    }
    const tbody=$('paperRows');tbody.replaceChildren();
    if(rows.length===0){
      const tr=elem('tr');
      tr.append(elem('td',{colspan:'9',className:'placeholder'},'Yeni LONG/SHORT sinyali oluşunca sanal takip başlayacak.'));
      tbody.append(tr);
      return;
    }
    for(const r of rows){const row=elem('tr');const st=elem('td');st.append(signalTag(r.signal));
      row.append(elem('td',{},r.symbol),st,elem('td',{},dt(r.observed_at)),elem('td',{},price(r.entry_price)),elem('td',{},price(r.target_price)),elem('td',{},price(r.stop_price)),elem('td',{},price(r.result_price)),
        elem('td',{className:r.net_return===null?'muted':r.net_return>=0?'positive':'negative'},r.net_return===null?'—':pct(r.net_return*100)),
        elem('td',{},r.legacy?(r.state==='CLOSED'?'Eski süreli hesap (hariç)':'Eski yöntem · dahil değil'):r.state==='VERI_EKSIK'?'Veri kesintisi · doğrulanamadı':r.state==='AWAITING_FUNDING'?((r.shadow?'TEST ADAYI · ':'')+'TP/SL görüldü · funding doğrulanıyor'):r.state==='CLOSED'?((r.shadow?'TEST ADAYI · ':'')+(r.exit_reason==='TP'?'TP DEĞDİ':r.exit_reason==='SL'?'SL DEĞDİ':'Sonuç')):((r.shadow?'TEST ADAYI · ':'')+'TP/SL bekleniyor · süre sınırı yok')));
      tbody.append(row)
    }
  }catch(e){setError('Sanal gözlemler alınamadı: '+e.message)}
}

for(let btn of document.querySelectorAll('.nav'))btn.addEventListener('click',()=>navigate(btn.dataset.page));
for(let btn of document.querySelectorAll('[data-goto]'))btn.addEventListener('click',()=>navigate(btn.dataset.goto));
$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadMarkets,350)});
$('quickPlanRefresh').addEventListener('click',loadOpportunities);
$('sortBy').addEventListener('change',loadMarkets);$('signalFilter').addEventListener('change',loadMarkets);$('refresh').addEventListener('click',async()=>{await Promise.all([loadHealth(),loadMarkets(),loadModel(),loadOpportunities()]);if(state.page==='details')loadDetail()});
$('loadMore').addEventListener('click',()=>{state.visible+=state.pageSize;paintSignals()});
$('paperRefresh').addEventListener('click',loadPaper);
$('qualityRefresh').addEventListener('click',loadQuality);
$('symbolSelect').addEventListener('change',e=>{state.selected=e.target.value;loadDetail()});
$('candlePeriod').addEventListener('change',e=>{state.period=e.target.value;loadDetail()});
async function refreshAll(){await Promise.all([loadHealth(),loadMarkets(),loadModel(),loadOpportunities()]);if(state.page==='details')await loadDetail();if(state.page==='paper')await loadPaper();if(state.page==='quality')await loadQuality()}
refreshAll();connectPriceStream();setInterval(()=>{if(!document.hidden)refreshAll()},30000);
