/* Рейс-контроль: панель флагов, лента инцидентов и панель решения.

   Один код на два места: вкладка «Рейс-контроль» главного окна и отдельное
   окно /racecontrol для второго монитора. Страница-хозяин подключает
   irc.js и этот файл, вызывает irc.rc.init({draw, active}) и передаёт сюда
   то, что приходит из потока: setState, setIncidents, setLive. Разметку
   берёт через irc.rc.html() и сама кладёт её через irc.setHTML.

   Клавиши (только пока рейс-контроль на экране):
     ↑ ↓ — инцидент, Space — повтор, Z / X / C — исход,
     Q / W / E / R — наказание (второе нажатие подтверждает),
     Enter — отправить маршалу, Y — жёлтый, L — к LIVE, Esc — отменить выбор.
*/
(function(){
'use strict';

const esc = irc.esc;
const PEN = [['drive_through', 'Проезд', 'Q'], ['time_penalty', 'Штраф', 'W'],
             ['eol', 'В конец колонны', 'E'], ['dq', 'Дисквалификация', 'R']];
const PEN_NAME = Object.fromEntries(PEN.map(([k, t]) => [k, t]));
const PEN_KEY = Object.fromEntries(PEN.map(([k, , key]) => [key, k]));

const S = {
  incidents: [], st: null, live: null, loaded: false,
  filter: 'all', cur: 0, edit: 0,
  pick: {outcome: '', car: '', armed: ''},
  sec: 5, clearArm: false,
  draw: () => {}, active: () => true,
};

const rcStatus = () => (S.st && (S.st.modules || []).find(m => m.id === 'racecontrol')) || {};
const liveStatus = () => S.live || rcStatus().live || null;
const pad = v => String(v).padStart(2, '0');
function clock(s){
  s = Math.max(0, s|0);
  const h = s/3600|0, m = s%3600/60|0, x = s%60;
  return h ? `${h}:${pad(m)}:${pad(x)}` : `${pad(m)}:${pad(x)}`;
}
const what = i => i.kind === 'offtrack' ? `вне трассы ${(i.duration || 0).toFixed(0)} с` : `+${i.inc_delta}x`;

function decTag(i){
  const d = i.decision_detail || {};
  if(i.decision === 'penalty')
    return irc.pill(`${PEN_NAME[d.penalty] || 'Наказание'}${d.seconds ? ' ' + d.seconds + ' с' : ''} · #${d.car}`, 'var(--hit-text)');
  if(i.decision === 'racing') return irc.pill('Гоночный', 'var(--dim)');
  if(i.decision === 'review') return irc.pill('После гонки', 'var(--warn)');
  return irc.pill('Новый', 'var(--acc)');
}
const syncTag = i => !i.decision ? ''
  : i.synced ? '<span class="tag" style="color:var(--on)" title="решение в Судействе">✓</span>'
  : '<span class="tag faint" title="ждёт отправки на сайт">○</span>';

function visible(){
  const f = S.filter;
  return S.incidents.filter(i => f === 'all' || (f === 'new' && !i.decision)
    || (f === 'done' && (i.decision === 'racing' || i.decision === 'penalty'))
    || (f === 'rev' && i.decision === 'review'));
}
const current = () => visible().find(i => i.id === S.cur) || null;
function resetPick(){ S.pick = {outcome: '', car: '', armed: ''}; }

// выбранный инцидент пропал из ленты или фильтра — встаём на первый неразобранный
function keepCurrent(){
  if(current()) return;
  const v = visible();
  const next = v.find(i => !i.decision) || v[0];
  if(next && next.id !== S.cur){ S.cur = next.id; S.edit = 0; resetPick(); }
}

/* ======================= разметка ======================= */

function toolbar(){
  const rc = rcStatus();
  const replay = S.st && S.st.mode === 'REPLAY';
  return `<section class="p rc-bar"><div class="pb row" style="flex-direction:row;flex-wrap:wrap;padding:10px 14px">
    <button class="k warn" data-rc="yellow">Жёлтый<kbd>Y</kbd></button>
    <span class="row" style="gap:10px;flex:none"><span class="lbl">Кругов под жёлтым</span>
      <span class="seg"><button type="button" data-rc="pace" data-v="-1">−</button><button type="button" aria-pressed="true"
        class="num" style="min-width:40px;pointer-events:none">${rc.pace_laps ?? '—'}</button><button type="button" data-rc="pace" data-v="1">+</button></span></span>
    <button class="k ${S.clearArm ? 'arm' : ''}" data-rc="clearall">${S.clearArm ? 'Ещё раз — снять со всех' : 'Снять все ЧФ'}</button>
    ${replay ? `<span class="tag" style="color:var(--warn)" title="в повторе соседей по трассе не видно">■ повтор · новых за это время: ${rc.missed_in_replay || 0}</span>` : ''}
    <span class="grow"></span>
    <button class="k ${rc.overlay_visible ? 'sel' : ''}" data-rc="overlay" title="окно рейс-контроля поверх iRacing">Поверх сима</button>
    <button class="k" data-rc="mark" data-v="0" title="предыдущий маркер инцидента iRacing">‹ Маркер iR</button>
    <button class="k" data-rc="mark" data-v="1" title="следующий маркер инцидента iRacing">Маркер iR ›</button>
    <button class="k" data-rc="cam" title="камера туда, где инцидент по мнению сима">Камера</button>
    <button class="k pri" data-rc="live">К LIVE<kbd>L</kbd></button>
  </div></section>`;
}

function stageHtml(){
  const l = liveStatus();
  if(!l) return '';
  const off = l.offline ? '<span class="tag" style="color:var(--warn)">нет связи с сайтом</span>' : '';
  if(!l.linked) return '<span class="tag" style="color:var(--warn)">нет входа — решения не уйдут в Судейство</span>';
  if(l.stage) return `<span class="lbl">Этап</span><span class="tag ell" title="${esc(l.stage.track || '')}">${
    esc(l.stage.championship)} · ${esc(l.stage.title)}</span>${off}`;
  if(!l.subsession_id) return '<span class="dim ell">этап определится, когда в симе будет сессия с номером</span>';
  if((l.candidates || []).length) return `<select class="inp text" id="rc-stage" style="width:auto;max-width:340px">${
    l.candidates.map(c => `<option value="${c.id}">${esc(c.championship)} · ${esc(c.title)}${c.track ? ' · ' + esc(c.track) : ''}${
      c.start_at ? ' · ' + new Date(c.start_at).toLocaleString('ru-RU', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'}) : ''}</option>`).join('')
    }</select><button class="k s pri" data-rc="stage">Судить этот этап</button>`;
  return `<span class="tag ell" style="color:var(--warn)">${esc(l.message || 'этап не определён')}</span>${off}`;
}

function emptyHint(){
  const st = S.st || {};
  if(!st.connected) return 'Инцидентов пока нет. Программа ждёт сессию iRacing.';
  if(!st.results_available) return `Тест-драйв: видны только свои инциденты (сейчас ${st.player_incidents || 0}x).`;
  return S.filter === 'all' ? 'Инцидентов пока нет. Поле под наблюдением.' : 'Здесь пусто.';
}

const COLS = '56px 28px 44px minmax(120px,1fr) minmax(70px,96px) minmax(110px,150px)';
function listHtml(){
  const v = visible();
  const fresh = S.incidents.filter(i => !i.decision).length;
  const st = S.st || {};
  const kick = st.connected ? [st.mode === 'REPLAY' ? 'повтор' : 'live', st.track, st.drivers ? st.drivers + ' машин' : ''].filter(Boolean).join(' · ')
    : 'ждёт сессию iRacing';
  return `<section class="p fit">${irc.ph(kick, 'Инциденты', [['всего', S.incidents.length], ['новых', fresh, fresh ? 'var(--acc)' : '']])}
    <div class="row" style="padding:8px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap">${
      irc.seg('rc-filter', [['all', 'Все'], ['new', 'Новые'], ['done', 'Решено'], ['rev', 'После гонки']], S.filter)}
      <span class="grow"></span>${stageHtml()}</div>
    <div class="tr h" style="grid-template-columns:${COLS}"><span>Время</span><span class="ar">Кр</span><span>№</span><span>Пилот</span><span>Что</span><span>Решение</span></div>
    <div class="scroll" style="flex:1" id="rc-list">${v.map(i => { const cur = i.id === S.cur;
      return `<div class="tr click ${cur ? 'cur' : ''}" data-key="${i.id}" data-rc="pick" data-v="${i.id}" style="grid-template-columns:${COLS}">
        <span class="bar" style="background:${cur ? 'var(--acc)' : i.decision ? 'transparent' : 'var(--air)'}"></span>
        <span class="num dim">${clock(i.session_time)}</span><span class="num ar">${i.lap}</span>
        <span>${irc.plate(i.car_number)}</span>
        <span class="nm" ${i.decision ? 'style="color:var(--dim)"' : ''}>${esc(i.driver_name)}${
          i.estimated ? ' <span class="tag" style="color:var(--best)" title="время эпизода оценено по повтору">≈</span>' : ''}</span>
        <span class="tag" style="color:${i.kind === 'offtrack' ? 'var(--off-track)' : 'var(--hit-text)'}">${esc(what(i))}</span>
        <span class="row">${decTag(i)}${syncTag(i)}</span></div>`; }).join('')
      || `<div class="pb dim">${emptyHint()}</div>`}</div></section>`;
}

function decisionHtml(i){
  if(!i) return `<section class="p fit" data-key="dec-none">${irc.ph('', 'Решение')}
    <div class="pb dim">Выберите инцидент в ленте — стрелками ↑ ↓ или мышью.</div></section>`;
  const d = i.decision_detail || {};
  const near = (i.nearby || []).length ? 'Рядом: ' + i.nearby.map(n => '#' + esc(n)).join(', ') : 'Рядом никого не было';
  let body = `<div class="row" style="min-width:0">${irc.plate(i.car_number)}<span class="nm grow" style="font-size:13.5px;font-weight:800">${esc(i.driver_name)}</span></div>
    <div class="dim">${esc(what(i))} · ${near}${i.kind === 'offtrack' ? '' : ' · всего ' + i.inc_total + 'x'}</div>
    <div class="row" style="flex-wrap:wrap"><button class="k s pri" data-rc="show">▶ Повтор<kbd>Space</kbd></button>
      <button class="k s" data-rc="cam" title="камера туда, где инцидент по мнению сима">Камера</button></div>`;
  if(i.decision && S.edit !== i.id){
    body += `<div style="border:1px solid var(--line2);border-radius:var(--r-m);padding:12px;display:flex;flex-direction:column;gap:8px">
      <div class="lbl">Решение</div>
      <div class="row">${decTag(i)}<span class="grow"></span><span class="tag" style="color:${i.synced ? 'var(--on)' : 'var(--faint)'}">${
        i.synced ? '✓ в Судействе' : 'ждёт отправки на сайт'}</span></div>
      ${d.note ? `<div class="dim">${esc(d.note)}</div>` : ''}
      ${i.decision === 'penalty' ? '<div class="faint">Наказание выдано в гонке — отменить его можно только в симе.</div>'
        : '<div><button class="k s" data-rc="edit">Изменить решение</button></div>'}</div>`;
  }else{
    const o = S.pick.outcome;
    const key = (v, t, k, cls) => `<button class="k l kc ${o === v ? (v === 'penalty' ? 'air' : 'sel') : cls}" data-rc="outcome" data-v="${v}" title="${t}"><span class="lb">${t}</span><kbd>${k}</kbd></button>`;
    body += `<div class="lbl">Исход</div>
      <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px">${
        key('racing', 'Гоночный', 'Z', '')}${key('penalty', 'Наказать', 'X', 'hit')}${key('review', 'После гонки', 'C', 'warn')}</div>`;
    if(o === 'penalty'){
      const cars = [i.car_number, ...(i.nearby || [])];
      body += `<div class="lbl">Кого</div>
        <div class="row" style="flex-wrap:wrap">${cars.map(c =>
          `<button class="k s ${S.pick.car === String(c) ? 'sel' : ''}" data-rc="car" data-v="${esc(c)}">#${esc(c)}</button>`).join('')}</div>
        <div class="lbl">Что · второе нажатие подтверждает</div>
        <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px">${PEN.map(([k, t, kk]) =>
          `<button class="k kc ${S.pick.armed === k ? 'arm' : 'hit'}" data-rc="pen" data-v="${k}"><span class="lb">${
            S.pick.armed === k ? 'Подтвердить: ' : ''}${t}${k === 'time_penalty' ? ' ' + S.sec + ' с' : ''}</span><kbd>${kk}</kbd></button>`).join('')}</div>
        <div class="row"><span class="lbl grow">Штраф по времени, секунд</span>
          <button class="k s" data-rc="sec" data-v="-1">−</button>
          <span class="num" style="width:32px;text-align:center;font-size:15px">${S.sec}</span>
          <button class="k s" data-rc="sec" data-v="1">+</button></div>
        <div class="lbl">Заметка для Судейства</div>
        <input class="inp text" id="rc-note" placeholder="необязательно">`;
    }else if(o === 'review'){
      body += `<div class="lbl">Что посмотреть маршалу</div>
        <input class="inp text" id="rc-note" placeholder="смотреть онборд, контакт в шикане…">
        <div><button class="k pri" data-rc="sendrev">Отправить маршалу<kbd>Enter</kbd></button></div>`;
    }else{
      body += `<div class="faint">Гоночный — никто не наказан, запись уходит в журнал этапа.
        Наказать — выдать наказание прямо в гонке. После гонки — карточка маршалу в «Судействе».</div>`;
    }
    if(S.edit === i.id) body += '<div><button class="k s" data-rc="cancel">Не менять<kbd>Esc</kbd></button></div>';
  }
  const stt = i.decision === 'penalty' ? 'наказан' : i.decision === 'racing' ? 'гоночный' : i.decision === 'review' ? 'после гонки' : 'новый';
  return `<section class="p fit" data-key="dec-${i.id}">${irc.ph(`${stt} · круг ${i.lap} · ${clock(i.session_time)}`, `Инцидент #${i.id}`)}
    <div class="pb scroll" style="flex:1">${body}</div></section>`;
}

function html(){
  keepCurrent();
  return `<div class="pg" data-key="racecontrol" style="grid-template-rows:auto minmax(0,1fr)">
    ${toolbar()}
    <div style="display:grid;grid-template-columns:minmax(0,1.55fr) minmax(380px,1fr);grid-template-rows:minmax(0,1fr);gap:8px;min-height:0;align-items:start">
      ${listHtml()}${decisionHtml(current())}</div></div>`;
}

/* ======================= действия ======================= */

const noteVal = () => (document.getElementById('rc-note') || {}).value || '';

async function flag(action, extra){
  const r = await irc.post('/api/racecontrol/flag', Object.assign({action}, extra || {}));
  if(r.ok && r.pace_laps !== undefined) rcStatus().pace_laps = r.pace_laps;
  return r;
}

async function decide(i, body){
  const r = await irc.post('/api/racecontrol/decide', Object.assign({id: i.id}, body));
  if(!r.ok) return;
  if(body.outcome === 'racing') irc.toast(`#${i.car_number}: гоночный инцидент`, 'ok');
  else if(body.outcome === 'review') irc.toast(`#${i.car_number}: маршалу после гонки`, 'wa');
  else irc.toast(`${PEN_NAME[body.penalty]}${body.penalty === 'time_penalty' ? ' ' + body.seconds + ' с' : ''} · #${body.car} — выдано в гонке`, 'er');
  // решение видно сразу, не дожидаясь ленты из программы
  Object.assign(i, {decision: body.outcome, synced: 0,
    decision_detail: {penalty: body.penalty, seconds: body.penalty === 'time_penalty' ? body.seconds : 0,
                      car: body.car || i.car_number, note: body.note || ''}});
  S.edit = 0; resetPick();
  const next = visible().find(x => !x.decision);
  if(next) S.cur = next.id;
  S.draw();
  scrollToCurrent();
}

function scrollToCurrent(){
  const el = document.querySelector(`#rc-list [data-v="${S.cur}"]`);
  if(el) el.scrollIntoView({block: 'nearest'});
}

async function act(a, v){
  const i = current();
  let r;
  switch(a){
    case 'pick':
      if(+v === S.cur) return;
      S.cur = +v; S.edit = 0; resetPick(); break;
    case 'move': {
      const list = visible(); if(!list.length) return;
      const ix = list.findIndex(x => x.id === S.cur);
      const n = list[Math.min(list.length - 1, Math.max(0, ix + v))];
      if(n.id === S.cur) return;
      S.cur = n.id; S.edit = 0; resetPick(); S.draw(); scrollToCurrent(); return; }
    case 'outcome':
      if(!i) return;
      if(i.decision === 'penalty'){ irc.toast('Наказание уже выдано — его не изменить', 'er'); return; }
      if(i.decision && S.edit !== i.id){ irc.toast('Решение уже есть — «Изменить решение», чтобы поменять', 'wa'); return; }
      if(v === 'racing'){ await decide(i, {outcome: 'racing', note: noteVal()}); return; }
      S.pick = {outcome: S.pick.outcome === v ? '' : v, car: v === 'penalty' ? String(i.car_number) : '', armed: ''};
      break;
    case 'car': S.pick.car = v; S.pick.armed = ''; break;
    case 'pen':
      if(!i || S.pick.outcome !== 'penalty') return;
      if(S.pick.armed !== v){
        // наказание в живой гонке не отменить — второе нажатие подтверждает
        S.pick.armed = v;
        setTimeout(() => { if(S.pick.armed === v){ S.pick.armed = ''; S.draw(); } }, 3500);
        break;
      }
      await decide(i, {outcome: 'penalty', penalty: v, car: S.pick.car || String(i.car_number),
                       seconds: S.sec, note: noteVal()});
      return;
    case 'sec': S.sec = Math.max(1, Math.min(120, S.sec + Number(v))); S.pick.armed = ''; break;
    case 'sendrev':
      if(!i || S.pick.outcome !== 'review') return;
      await decide(i, {outcome: 'review', note: noteVal()}); return;
    case 'edit': if(i){ S.edit = i.id; resetPick(); } break;
    case 'cancel': S.edit = 0; resetPick(); break;
    case 'show':
      if(!i) return;
      r = await irc.post('/api/racecontrol/show', {id: i.id});
      if(r.ok) irc.toast(`Повтор: #${i.car_number}, круг ${i.lap}${i.estimated ? ' · время оценочное, отмотка с запасом' : ''}`, 'ok');
      return;
    case 'live':
      r = await irc.post('/api/racecontrol/live');
      if(r.ok !== false) irc.toast('Вернулись в прямой эфир');
      return;
    case 'mark': irc.post('/api/racecontrol/command', {name: 'jump_sim_incident', forward: v === '1'}); return;
    case 'cam': irc.post('/api/racecontrol/command', {name: 'cam_at_incident'}); return;
    case 'yellow':
      r = await flag('yellow');
      if(r.ok) irc.toast(`Жёлтый флаг · кругов за пейс-каром: ${rcStatus().pace_laps ?? '—'}`, 'wa');
      return;
    case 'pace': await flag('pace_laps', {delta: Number(v)}); break;
    case 'clearall':
      if(!S.clearArm){
        S.clearArm = true;
        setTimeout(() => { if(S.clearArm){ S.clearArm = false; S.draw(); } }, 4000);
        break;
      }
      S.clearArm = false;
      r = await flag('clear_all');
      if(r.ok) irc.toast('Чёрные флаги сняты со всех', 'ok');
      break;
    case 'overlay': {
      const on = !rcStatus().overlay_visible;
      r = await irc.post('/api/racecontrol/overlay', {visible: on});
      if(r.ok){ rcStatus().overlay_visible = on; irc.toast(on ? 'Рейс-контроль поверх сима' : 'Окно поверх сима скрыто'); }
      break; }
    case 'stage': {
      const sel = document.getElementById('rc-stage'); if(!sel) return;
      r = await irc.post('/api/racecontrol/stage', {stage_plan_id: +sel.value});
      if(r.ok) irc.toast('Этап выбран — решения уйдут в Судейство', 'ok');
      return; }
    default: return;
  }
  S.draw();
}

document.addEventListener('click', e => {
  const el = e.target.closest('[data-rc]');
  if(!el || el.disabled || !S.active()) return;
  act(el.dataset.rc, el.dataset.v);
});
irc.on('seg', (key, v) => {
  if(key !== 'rc-filter' || !S.active()) return;
  S.filter = v; S.draw();
});
// Enter в поле заметки отправляет маршалу
document.addEventListener('keydown', e => {
  if(e.key === 'Enter' && e.target.id === 'rc-note' && S.active() && S.pick.outcome === 'review'){
    e.preventDefault(); act('sendrev');
  }
});

const when = fn => e => { if(!S.active()) return false; return fn(e); };
irc.keys({
  'ArrowDown': when(() => act('move', 1)),
  'ArrowUp': when(() => act('move', -1)),
  'Space': when(() => act('show')),
  'Z': when(() => act('outcome', 'racing')),
  'X': when(() => act('outcome', 'penalty')),
  'C': when(() => act('outcome', 'review')),
  'Q': when(() => S.pick.outcome === 'penalty' ? act('pen', PEN_KEY.Q) : false),
  'W': when(() => S.pick.outcome === 'penalty' ? act('pen', PEN_KEY.W) : false),
  'E': when(() => S.pick.outcome === 'penalty' ? act('pen', PEN_KEY.E) : false),
  'R': when(() => S.pick.outcome === 'penalty' ? act('pen', PEN_KEY.R) : false),
  'Enter': when(() => S.pick.outcome === 'review' ? act('sendrev') : false),
  'Y': when(() => act('yellow')),
  'L': when(() => act('live')),
  'Escape': when(() => {
    if(!S.pick.outcome && !S.edit) return false;
    S.edit = 0; resetPick(); S.draw();
  }),
});

/* ======================= данные ======================= */

async function load(){
  const [list, live] = await Promise.all([irc.get('/api/racecontrol/incidents'), irc.get('/api/racecontrol/stage')]);
  if(Array.isArray(list)){ S.incidents = list; S.loaded = true; }
  if(live && live.ok !== false) S.live = live;
  S.draw();
}

irc.rc = {
  init(opts){ Object.assign(S, opts || {}); load(); },
  html,
  load,
  setState(st){
    const was = rcStatus().started;
    S.st = st;
    if(rcStatus().started && !was && S.loaded) load();   // модуль только что включили
  },
  setIncidents(list){ S.incidents = list || []; S.loaded = true; S.draw(); },
  setLive(l){ S.live = l; S.draw(); },
};
})();
