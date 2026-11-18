/* Пульт эфира: клавиши оверлеев, камеры, таблица пилотов, макросы.

   Один код на три места: вкладка «Пульт» главного окна, окно поверх сима
   и страница /deck на телефоне или втором ПК. Хозяин подключает irc.js и
   этот файл, вызывает irc.deck.init({draw, active, airActive, side}) и
   кладёт irc.deck.html() через irc.setHTML. Данные пульт опрашивает сам,
   пока он на экране.

   Клавиши: F1–F8 — оверлей в эфир / из эфира, Esc — убрать всё,
   1–8 — избранные камеры, «/» — к поиску пилота, Enter в поиске — камера на него.

   Камеры: на трассе их бывает 15–25 групп. В пульте — до восьми избранных
   (клавиши 1–8), остальные — в «Все камеры» по группам; звёздочка добавляет
   в избранное. Набор хранится в настройках трансляции (cam_favorites) — общий
   для окна, пульта поверх сима и телефона.

   Телефон (уже 700 px): одна панель на экран и нижнее меню
   «Эфир · Камеры · Пилоты · Макросы».
*/
(function(){
'use strict';

const esc = irc.esc;
const SHORT = {tower: 'Башня', grid: 'Решётка', commentators: 'Комментаторы', delta: 'Дельта', pits: 'Питы',
               weather: 'Погода', events: 'События', bestlap: 'Лучший круг'};
// избранные по умолчанию — то, чем режиссёр обычно работает в гонке
const DEF_FAVS = ['TV1', 'TV2', 'TV3', 'Chase', 'Far Chase', 'Cockpit', 'Blimp', 'Chopper', 'Pit Lane'];
const MAX_FAVS = 8;
// группы камер для полного списка: по имени группы из iRacing
const CAM_KIND = [['ТВ', /^(tv|scenic|blimp|chopper|heli)/i], ['Погоня', /chase/i],
                  ['Пит-лейн', /pit/i],
                  ['Бортовые', /cockpit|nose|gearbox|roll|susp|gyro|rear|wing|chassis|mirror|bumper|cam/i]];
// цвета классов — те же, что у чипов в эфирной графике (style.py)
const CLASS = {GT3: '#FF5A1F', PRO: '#FF5A1F', GT4: '#3B82F6', AM: '#3B82F6', SILVER: '#94A3B8', GOLD: '#E3B341'};
const clsColor = c => CLASS[String(c || '').toUpperCase()] || '#E8E8EC';

const S = {
  overlays: [], cams: {groups: [], current: {}}, cards: null,
  macros: [], running: null, bcOn: false, q: '',
  favs: null,                    // избранные камеры: null — ещё не загружены, [] — не заданы
  dir: null,                     // режиссёр: инциденты, повтор, автокамеры (/api/broadcast/director)
  allCams: false,                // открыт полный список камер
  sect: 'air',                   // телефон: какая панель на экране
  draw: () => {}, active: () => true, airActive: null, side: () => '',
};

/* ======================= форматирование ======================= */

function surname(n){
  const p = String(n || '').trim().split(/\s+/).filter(Boolean);
  if(!p.length) return '';
  let t = p[p.length - 1];
  if(t.length <= 2 && p.length > 1) t = p[0];          // инициал в конце
  return t.toUpperCase();
}
function fmtT(t, d = 3){
  if(t == null || t <= 0) return '—';
  if(t >= 60){ const m = Math.floor(t / 60); return `${m}:${(t - m * 60).toFixed(d).padStart(d + 3, '0')}`; }
  return t.toFixed(d);
}
const fmtGap = g => g == null ? '' : '+' + g.toFixed(1);
function secColor(t, i, best, overall){
  if(t == null) return 'var(--faint)';
  if(overall[i] != null && t <= overall[i] + 0.0005) return 'var(--best)';
  if(best[i] != null && t <= best[i] + 0.0005) return 'var(--on)';
  return 'var(--warn)';
}
// номер ищем с начала («1» даёт 1, 12, 14), фамилию и команду — вхождением
function matches(r, q){
  if(!q) return true;
  const qn = q.replace(/^#/, '');
  if(/^\d+$/.test(qn)) return String(r.car_number).startsWith(qn);
  return (r.name + ' ' + (r.team || '')).toLowerCase().includes(q.toLowerCase());
}
function hl(text, q){
  if(!q || /^#?\d+$/.test(q)) return esc(text);
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if(i < 0) return esc(text);
  return esc(text.slice(0, i)) + '<mark>' + esc(text.slice(i, i + q.length)) + '</mark>' + esc(text.slice(i + q.length));
}
const visibleRows = () => (S.cards && S.cards.rows || []).filter(r => matches(r, S.q.trim()));

/* ======================= разметка ======================= */

const CSS = `
.dk{grid-template-columns:minmax(0,1fr) 288px}
.dk-main{display:grid;grid-template-rows:auto auto auto minmax(0,1fr);gap:8px;min-height:0;min-width:0}
.dk-air{display:grid;grid-template-columns:repeat(auto-fill,minmax(124px,1fr));gap:8px}
.dk-air .k,.dk-cams .k{position:relative;overflow:hidden;min-width:0;padding-right:12px}
/* подсказка клавиши — в углу: длинной подписи («Комментаторы») нужна вся ширина */
.dk-air .k kbd,.dk-cams .k kbd{position:absolute;top:3px;right:3px;border:0;padding:0;line-height:1}
.dk-air .k .lb,.dk-cams .k .lb{min-width:0;overflow:hidden;text-overflow:ellipsis}
.dk-cams{display:grid;grid-template-columns:repeat(auto-fill,minmax(112px,1fr));gap:8px}
.dk-allc .lbl{font:800 10.5px var(--f-ui);letter-spacing:.16em;text-transform:uppercase;color:var(--faint);margin:4px 0 6px}
.dk-allc .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px}
.dk-cr{display:flex;gap:4px;min-width:0}
.dk-cr .k{min-width:0;flex:1;justify-content:flex-start}
.dk-cr .k.star{flex:none;width:36px;padding:0;justify-content:center;font-size:14px;color:var(--faint)}
.dk-cr .k.star.on{color:var(--acc)}
/* широкое окно (от 1280 px): эфир — узкой колонкой слева, пилоты — на всю
   высоту в центре, камеры и макросы — справа. Раньше эфир и камеры стояли над
   таблицей, и на ней оставалось три-четыре строки. */
.dk3{grid-template-columns:230px minmax(0,1fr) 300px}
/* боковая колонка: в невысоком окне (пульт поверх сима) прокручивается
   целиком — макросы не сжимаются до одного заголовка */
.dk-col{display:flex;flex-direction:column;gap:8px;min-height:0;min-width:0;overflow-y:auto}
.dk-col::-webkit-scrollbar{width:6px} .dk-col::-webkit-scrollbar-thumb{background:var(--line2)}
.dk-col > .p{flex:none}
.ph .k.dk-hb{height:24px;padding:0 8px;font-size:10px;margin-left:8px;flex:none}
.ph .k.dk-hb kbd{position:static;border:0;padding:0}
.dk2 .dk-col .note{display:none}
/* узкая колонка эфира: «эфир пуст / в эфире» уже есть над пультом и внизу окна */
.dk3 .dk-col:first-child .ph .r{display:none}
.dk-col > .p.grow{flex:1 0 auto;min-height:190px}
/* окно 1000–1279 px: пилоты слева на всю высоту, справа эфир, камеры, макросы */
.dk2{grid-template-columns:minmax(0,1fr) 300px}
.dk3 .dk-air{grid-template-columns:minmax(0,1fr);gap:6px}
.dk3 .dk-air .k{height:38px}
.dk2 .dk-air{grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
.dk2 .dk-air .k{height:34px}
.dk3 .dk-cams,.dk2 .dk-cams{grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
.dk3 .dk-cams .k{height:36px}
.dk2 .dk-cams .k{height:34px}
.dk-phone{display:flex;flex-direction:column;height:100%;gap:8px}
.dk-sect{flex:1;min-height:0;display:flex;flex-direction:column;overflow-y:auto}
.dk-sect > .p{flex:1;min-height:0}
.dk-sect > .p.fit{flex:none}
.dk-nav{flex:none;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px}
.dk-nav button{height:52px;border-radius:var(--r-s);background:var(--raise);border:1px solid var(--line2);
               font:800 11px var(--f-ui);letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
.dk-nav button[aria-pressed="true"]{background:var(--fg);color:#000;border-color:var(--fg)}
.dk-nav button .n{display:inline-block;margin-left:4px;min-width:16px;padding:0 4px;border-radius:8px;background:var(--air);color:#fff;font:700 10px var(--f-num)}
.dk-phone .dk-air{grid-template-columns:repeat(2,minmax(0,1fr))}
.dk-phone .dk-air .k{height:64px;font-size:13px}
.dk-phone .dk-cams{grid-template-columns:repeat(2,minmax(0,1fr))}
.dk-phone .dk-cams .k{height:56px}
.dk mark{background:var(--acc);color:#000}
.dk-inc{display:flex;flex-direction:column;gap:2px;max-height:260px;min-height:0}
.dk-ir{display:grid;grid-template-columns:28px 34px minmax(0,1fr) auto auto;align-items:center;gap:8px;height:32px;padding:0 4px 0 6px;border-radius:4px}
.dk-ir:hover{background:rgba(255,255,255,.03)}
.dk-ir.cur{background:linear-gradient(90deg,rgba(255,90,31,.20),rgba(255,90,31,.04))}
.dk-ir .k{height:24px;padding:0 9px;font-size:10px}
.dk-dset{display:flex;flex-direction:column;gap:8px;border-top:1px solid var(--line);padding-top:10px}
.dk-dset .row{flex-wrap:wrap;gap:6px}
.dk-dset .seg button{padding:0 8px;font-size:10px}
.dk-grp{display:grid;grid-template-columns:repeat(auto-fill,minmax(86px,1fr));gap:6px}
.dk-grp .k{min-width:0;justify-content:center;overflow:hidden}
.dk-grp .k .lb{overflow:hidden;text-overflow:ellipsis}
.dk .tr .sec{font:500 12px var(--f-num);text-align:right}
@media (max-width:999px){
  .dk{display:flex;flex-direction:column;overflow-y:auto}
  .dk-main{display:flex;flex-direction:column;gap:8px}
  .dk .p,.dk-main{flex:none}
  .dk-main > .p:last-child{height:70vh;min-height:360px}
  .dk-air{grid-template-columns:repeat(3,minmax(0,1fr))}
}
@media (max-width:699px){ .dk-air .k kbd,.dk-cams .k kbd{display:none} .dk-air .k{padding:0 10px;justify-content:center} }`;
(function(){ const st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st); })();

function airPanel(){
  const on = S.overlays.filter(o => o.visible).length;
  // на телефоне «Убрать всё» — крупной кнопкой под палец, в окне — в заголовке:
  // так панель на ряд ниже и в невысоком окне остаётся место макросам
  const big = PHONE();
  return `<section class="p fit"><div class="ph">Эфир<span class="r">${on ? 'в эфире: ' + on : 'эфир пуст'}</span>${
      big ? '' : '<button class="k s dk-hb" data-dk="clear" title="убрать всё из эфира">Убрать всё <kbd>Esc</kbd></button>'}</div>
    <div class="pb dk-air">${S.overlays.map((o, i) =>
      `<button class="k l full ${o.visible ? 'air' : ''}" data-dk="air" data-v="${esc(o.id)}" title="${esc(o.title)}">${
        `<span class="lb">${esc(SHORT[o.id] || o.title)}</span>`}<kbd>F${i + 1}</kbd></button>`).join('')}
      ${big ? '<button class="k l full" data-dk="clear"><span class="lb">Убрать всё</span><kbd>Esc</kbd></button>' : ''}</div></section>`;
}

// Избранные: заданные режиссёром, иначе — привычные по умолчанию из тех, что
// есть на трассе, добитые остальными до восьми. Камера, которой на этой трассе
// нет, остаётся неактивной кнопкой — раскладка не прыгает.
function favs(){
  const groups = S.cams.groups || [];
  const byName = n => groups.find(g => g.name === n);
  let names = S.favs && S.favs.length ? S.favs.slice(0, MAX_FAVS)
    : DEF_FAVS.filter(byName).concat(groups.map(g => g.name))
        .filter((n, i, a) => a.indexOf(n) === i).slice(0, MAX_FAVS);
  return names.map(n => ({name: n, g: byName(n)}));
}
function camKind(name){
  const k = CAM_KIND.find(([, re]) => re.test(name));
  return k ? k[0] : 'Прочие';
}

function camPanel(){
  const cur = S.cams.current || {};
  const groups = S.cams.groups || [];
  const big = PHONE();
  const head = `<div class="ph">Камера<span class="r">${cur.car_number
      ? `в кадре #${esc(cur.car_number)} · ${esc(cur.group_name || '')}` : 'клик по пилоту — камера на него'}</span>${
      !big && groups.length && !S.allCams ? `<button class="k s dk-hb" data-dk="allcams" title="все камеры трассы по группам">Все <kbd>${groups.length}</kbd></button>` : ''}</div>`;
  if(!groups.length) return `<section class="p">${head}<div class="pb"><div class="dim">Группы камер появятся, когда сим подключится.</div></div></section>`;
  if(S.allCams){
    const fav = new Set(favs().map(f => f.name));
    const order = ['ТВ', 'Погоня', 'Бортовые', 'Пит-лейн', 'Прочие'];
    const by = {};
    for(const g of groups) (by[camKind(g.name)] = by[camKind(g.name)] || []).push(g);
    return `<section class="p">${head}<div class="pb dk-allc scroll" style="max-height:60vh">
      <div class="row"><span class="dim grow">Все камеры трассы: ${groups.length}. ★ — в избранное (до ${MAX_FAVS}, клавиши 1–${MAX_FAVS}).</span>
        <button class="k s" data-dk="allcams">Готово</button></div>
      ${order.filter(k => by[k]).map(k => `<div><div class="lbl">${k}</div><div class="grid">${by[k].map(g =>
        `<div class="dk-cr"><button class="k s ${g.num === cur.group ? 'sel' : ''}" data-dk="cam" data-v="${g.num}" data-close="1" title="${esc(g.name)}"><span class="lb">${esc(g.name)}</span></button>
         <button class="k s star ${fav.has(g.name) ? 'on' : ''}" data-dk="fav" data-v="${esc(g.name)}" title="${fav.has(g.name) ? 'убрать из избранного' : 'в избранное'}">${fav.has(g.name) ? '★' : '☆'}</button></div>`).join('')}</div></div>`).join('')}
    </div></section>`;
  }
  return `<section class="p">${head}
    <div class="pb"><div class="dk-cams">${favs().map((f, i) =>
      `<button class="k full ${f.g && f.g.num === cur.group ? 'sel' : ''}" data-dk="cam" data-v="${f.g ? f.g.num : ''}" ${f.g ? '' : 'disabled'} title="${esc(f.name)}${f.g ? '' : ' — на этой трассе нет'}">${
        `<span class="lb">${esc(f.name)}</span>`}<kbd>${i + 1}</kbd></button>`).join('')}
      ${big ? `<button class="k full" data-dk="allcams" title="все камеры трассы по группам"><span class="lb">Все камеры</span><kbd>${groups.length}</kbd></button>` : ''}</div></div></section>`;
}

async function toggleFav(name){
  let list = favs().map(f => f.name);
  if(list.includes(name)) list = list.filter(n => n !== name);
  else if(list.length >= MAX_FAVS){ irc.toast(`Избранных не больше ${MAX_FAVS} — сначала уберите одну`, 'wa'); return; }
  else list.push(name);
  S.favs = list; S.draw();
  const r = await irc.post('/api/broadcast/settings', {cam_favorites: list}, true);
  if(r && r.ok === false) irc.toast('Избранное не сохранилось', 'er');
}

function driversPanel(){
  const d = S.cards;
  // на телефоне — только главное: сектора и времена кругов не помещаются
  const narrow = window.innerWidth < 700;
  // в трёх колонках таблице достаётся центр: лишние колонки убираем по его
  // ширине — сначала «к лидеру» и лучший круг, потом сектора
  const center = THREE() ? window.innerWidth - 562 : TWO() ? window.innerWidth - 324 : window.innerWidth;
  const full = !narrow && center >= 950;
  const n = narrow || center < 740 ? 0 : (d && d.sectors_n) || 0;
  const cols = narrow ? '20px 40px minmax(0,1fr) 52px 28px 28px 30px;column-gap:6px;padding-left:8px'
    : `32px 44px minmax(120px,1fr) 72px${full ? ' 72px' : ''}${' 64px'.repeat(n)} 80px${full ? ' 80px' : ''} 56px 48px 34px`;
  const pair = (d && d.delta_pair) || [];
  const wide = html => narrow ? '' : html;
  const wfull = html => full ? html : '';
  const all = d && d.rows || [];
  const q = S.q.trim();
  const rows = visibleRows();
  const overall = (d && d.best) || [];
  let body;
  if(!all.length) body = '<div class="pb dim">Пилотов нет: сим не подключён. Для проверки включите демо-гонку на «Обзоре».</div>';
  else if(!rows.length) body = `<div class="pb dim">Никого по запросу «${esc(q)}».</div>`;
  else body = rows.map(r => {
    const focus = r.car_idx === d.focus_car_idx;
    const leader = r.position === 1;
    const s = r.sectors || {};
    let secs = '';
    for(let i = 0; i < n; i++){
      let t = (s.current || [])[i], old = false;
      if(t == null){ t = (s.last || [])[i]; old = t != null; }
      secs += `<span class="sec" style="color:${secColor(t, i, s.best || [], overall)}${old ? ';opacity:.45' : ''}">${t == null ? '—' : fmtT(t)}</span>`;
    }
    const inc = r.incidents;
    // только что получил инки: подсветка по весу, гаснет сама
    const hit = r.inc_new ? (r.inc_new >= 4 ? '255,77,77' : r.inc_new >= 2 ? '255,90,31' : '255,210,0') : '';
    const flash = hit ? `;background:rgba(${hit},${(.34 * r.inc_fade).toFixed(3)})` : '';
    const inPair = pair.includes(r.car_number);
    return `<div class="tr click ${focus ? 'cur' : ''}" data-key="${esc(r.car_number)}" data-dk="car" data-v="${esc(r.car_number)}" style="grid-template-columns:${cols}${flash}" title="${esc(r.name)}${r.team ? ' · ' + esc(r.team) : ''}">
      <span class="bar" style="background:${focus ? 'var(--air)' : 'transparent'}"></span>
      <span class="num ar">${r.position}</span>
      <span><span class="cn" style="background:${clsColor(r.class_name)}">${esc(r.car_number)}</span></span>
      <span class="nm">${hl(surname(r.name), q)}</span>
      <span class="num ar" ${leader ? 'style="color:var(--acc)"' : ''}>${leader ? 'ЛИДЕР' : fmtGap(r.interval)}</span>
      ${wfull(`<span class="num ar dim">${leader ? '' : fmtGap(r.gap)}</span>`)}${secs}
      ${wide(`<span class="num ar">${fmtT(r.last_lap)}</span>`)}${wfull(`<span class="num ar dim">${fmtT(r.best_lap)}</span>`)}
      <span class="num ar" style="color:${hit ? `rgb(${hit})` : inc >= 8 ? 'var(--hit-text)' : 'var(--dim)'}">${r.inc_new ? `<b>+${r.inc_new}x</b> ` : ''}${inc == null ? '—' : inc + 'x'}</span>
      <span class="ar">${r.in_pit ? '<span class="tag" style="color:var(--off-track)">ПИТ</span>' : r.pit_stops ? `<span class="num dim">${r.pit_stops}</span>` : ''}</span>
      <span class="ar"><button class="k s" data-dk="dpair" data-v="${esc(r.car_number)}" title="Дельта: этот пилот и сосед впереди" style="padding:0 7px;height:22px;${inPair ? 'background:var(--acc);border-color:var(--acc);color:#000' : ''}">Δ</button></span></div>`;
  }).join('');
  let secHead = '';
  for(let i = 0; i < n; i++) secHead += `<span class="ar">S${i + 1}</span>`;
  return `<section class="p"><div class="ph">Пилоты<span class="r">${all.length
      ? (q ? `найдено ${rows.length} из ${all.length}` : `${all.length} · клик — камера на пилота`) : ''}</span></div>
    <div class="row" style="padding:8px 12px;border-bottom:1px solid var(--line)">
      <input class="inp text" id="dk-q" style="max-width:260px" placeholder="№ или фамилия — «/»" autocomplete="off">
      <span class="dim ell">Enter — камера на первого найденного, Esc — очистить</span></div>
    <div class="tr h" style="grid-template-columns:${cols}"><span class="ar">P</span><span>№</span><span>Пилот</span>
      <span class="ar">${narrow ? 'Инт' : 'Интервал'}</span>${wfull('<span class="ar">К лидеру</span>')}${secHead}${
      wide('<span class="ar">Посл.</span>')}${wfull('<span class="ar">Лучший</span>')}<span class="ar">Инц</span><span class="ar">Пит</span><span class="ar">Δ</span></div>
    <div class="scroll" style="flex:1">${body}</div></section>`;
}

/* Повторы и автокамеры: инциденты сессии с кнопкой «Повтор», возврат в LIVE,
   камера на инцидент и автосмена камер по кругу из выбранных групп. */
const INC_COL = d => d >= 4 ? 'var(--hit-text)' : d >= 2 ? 'var(--acc)' : 'var(--warn)';
function dirPanel(){
  const D = S.dir;
  if(!D) return `<section class="p fit"><div class="ph">Повторы</div><div class="pb dim">Загрузка…</div></section>`;
  const c = D.cfg || {}, rp = D.replay, busy = D.fx && D.fx.phase;
  const live = D.live && !rp;
  const list = (D.incidents || []).slice(0, 12);
  const names = favs().filter(f => f.g).map(f => f.name);
  const grp = new Set(c.auto_groups || []);
  return `<section class="p fit"><div class="ph">Повторы<span class="r">${busy ? 'шторка…' : rp ? `повтор · #${esc(rp.car_number)}` : live ? 'live' : 'повтор'}</span></div>
    <div class="pb">
      ${live ? '' : `<button class="k l full pri" data-dk="golive" ${busy ? 'disabled' : ''}><span class="lb">● Вернуться в LIVE</span><kbd>L</kbd></button>`}
      <div class="dk-inc scroll">${list.map(x => `<div class="dk-ir ${rp && rp.id === x.id ? 'cur' : ''}">
          <span class="num dim">L${x.lap}</span>${irc.plate ? irc.plate(x.car_number) : `<span class="cn">${esc(x.car_number)}</span>`}
          <span class="nm">${esc(surname(x.name))}</span><span class="tag" style="color:${INC_COL(x.delta)}">+${x.delta}x</span>
          <button class="k s" data-dk="replay" data-v="${x.id}" ${busy ? 'disabled' : ''} title="повтор с ${c.pre_roll || 6} с до инцидента, камера на #${esc(x.car_number)}">Повтор</button></div>`).join('')
        || '<div class="dim">Инцидентов пока нет — появятся по счётчикам инков iRacing.</div>'}</div>
      <div class="dk-dset">
        <div class="row"><button class="k s ${c.inc_cam ? 'sel' : ''}" data-dk="dset" data-v="inc_cam" title="новый инцидент — камера на эту машину">Камера на инцидент</button>
          <span class="seg">${[1, 2, 4].map(n => `<button type="button" data-dk="dmin" data-v="${n}" aria-pressed="${(c.inc_min || 2) === n}" title="${n === 1 ? 'все, и вылеты' : n === 2 ? 'от потери контроля' : 'только контакты'}">${n}x+</button>`).join('')}</span></div>
        <div class="row"><button class="k s ${c.auto_cam ? 'sel' : ''}" data-dk="dset" data-v="auto_cam" title="камеры меняются сами, пилотов переключаете вы">Автосмена камер</button>
          <span class="seg">${[8, 12, 15, 20].map(n => `<button type="button" data-dk="devery" data-v="${n}" aria-pressed="${(c.auto_every || 12) === n}">${n} с</button>`).join('')}</span></div>
        <div class="dk-grp">${names.map(n => `<button class="k s ${grp.has(n) ? 'sel' : ''}" data-dk="dgrp" data-v="${esc(n)}" title="${grp.has(n) ? 'убрать из автосмены' : 'в автосмену'}"><span class="lb">${esc(n)}</span></button>`).join('')
          || '<span class="dim">Камеры появятся, когда сим подключится.</span>'}</div>
        ${c.auto_cam && grp.size < 2 ? '<div class="dim">Выберите хотя бы две камеры.</div>'
          : c.auto_cam && D.next_cam_in != null ? `<div class="dim">следующая камера через ${Math.ceil(D.next_cam_in)} с</div>` : ''}
      </div></div></section>`;
}

function macroPanel(){
  return `<section class="p"><div class="ph">Макросы</div><div class="pb scroll">
    ${S.macros.map(m => `<button class="k l full ${S.running === m.id ? 'pri' : ''}" data-dk="macro" data-v="${esc(m.id)}">${esc(m.title)}${
      S.running === m.id ? '<kbd>идёт</kbd>' : ''}</button>`).join('') || '<div class="dim">Макросов пока нет.</div>'}
    <button class="k full" data-dk="editor">Редактор макросов</button>
    <div class="note dim">Макрос — цепочка шагов: камера, пауза, оверлей. Один клик — и графика с камерой меняются по сценарию.</div>
    ${S.side() || ''}</div></section>`;
}

const PHONE = () => window.innerWidth < 700;
const THREE = () => window.innerWidth >= 1280;      // эфир | пилоты | камеры и макросы
const TWO = () => window.innerWidth >= 1000 && !THREE();   // пилоты | эфир, камеры, макросы
const SECTS = [['air', 'Эфир'], ['dir', 'Повторы'], ['cams', 'Камеры'], ['drivers', 'Пилоты'], ['macros', 'Макросы']];

function html(){
  if(!S.bcOn) return '';
  if(PHONE()){
    // телефон: одна панель на экран, переключение — нижним меню под палец
    const on = S.overlays.filter(o => o.visible).length;
    const panel = {air: airPanel, dir: dirPanel, cams: camPanel, drivers: driversPanel, macros: macroPanel}[S.sect] || airPanel;
    return `<div class="pg dk dk-phone" data-key="deck-ph-${S.sect}"><div class="dk-sect">${panel()}</div>
      <nav class="dk-nav">${SECTS.map(([k, n]) => `<button data-dk="sect" data-v="${k}" aria-pressed="${S.sect === k}">${n}${
        k === 'air' && on ? `<span class="n">${on}</span>` : ''}</button>`).join('')}</nav></div>`;
  }
  if(THREE()){
    return `<div class="pg dk dk3" data-key="deck3"><div class="dk-col">${airPanel()}</div>${driversPanel()}
      <div class="dk-col">${dirPanel()}${camPanel()}${macroPanel().replace('<section class="p">', '<section class="p grow">')}</div></div>`;
  }
  if(TWO()){
    return `<div class="pg dk dk2" data-key="deck2">${driversPanel()}
      <div class="dk-col">${airPanel()}${dirPanel()}${camPanel()}${macroPanel().replace('<section class="p">', '<section class="p grow">')}</div></div>`;
  }
  return `<div class="pg dk" data-key="deck">
    <div class="dk-main">${airPanel()}${dirPanel()}${camPanel()}${driversPanel()}</div>${macroPanel()}</div>`;
}

/* ======================= действия ======================= */

async function toggle(id){
  const o = S.overlays.find(x => x.id === id); if(!o) return;
  const on = !o.visible;
  o.visible = on; S.draw();
  const r = await irc.post('/api/broadcast/visibility', {id, visible: on});
  if(r.overlays) S.overlays = r.overlays;
  else o.visible = !on;
  if(r.ok !== false) irc.toast(`${o.title}: ${on ? 'в эфире' : 'убран'}`, on ? 'er' : '');
  S.draw();
}
async function clear(){
  const on = S.overlays.filter(o => o.visible);
  if(!on.length) return;
  on.forEach(o => o.visible = false); S.draw();
  for(const o of on) await irc.post('/api/broadcast/visibility', {id: o.id, visible: false}, true);
  irc.toast('Эфир очищен');
  pollState();
}
async function camera(body, label){
  const r = await irc.post('/api/broadcast/camera', body, true);
  if(r.current){ S.cams.current = r.current; if(S.cards) S.cards.focus_car_idx = r.current.car_idx; }
  if(r.ok){ if(label) irc.toast(label); }
  else irc.toast('Камера не переключилась: сим не готов', 'er');
  S.draw();
}
// Дельта одной кнопкой: этот пилот и сосед впереди (лидер — со вторым), оверлей
// сразу в эфире; та же пара и она в эфире — убрать из эфира
async function deltaPair(num){
  const rows = (S.cards && S.cards.rows) || [];
  const k = rows.findIndex(r => r.car_number === num);
  if(k < 0 || rows.length < 2) return;
  const other = rows[k ? k - 1 : 1].car_number;
  const cur = S.cards.delta_pair || [];
  const same = cur.includes(num) && cur.includes(other);
  const on = (S.overlays.find(o => o.id === 'delta') || {}).visible;
  if(same && on){ toggle('delta'); return; }
  const r = await irc.post('/api/broadcast/settings', {delta_pair: [num, other]}, true);
  if(r && r.ok === false){ irc.toast('Пара не сохранилась', 'er'); return; }
  S.cards.delta_pair = [num, other];
  if(!on) toggle('delta'); else S.draw();
  irc.toast(`Дельта: #${other} и #${num}`, 'ok');
}

function pickSearch(){
  const q = S.q.trim().replace(/^#/, '');
  const rows = visibleRows();
  // точное совпадение номера важнее: «1» — это №1, а не №14, идущий выше
  const pick = rows.find(r => r.car_number === q) || rows[0];
  if(pick) camera({car: pick.car_number}, `Камера: #${pick.car_number}`);
}

document.addEventListener('click', e => {
  const el = e.target.closest('[data-dk]');
  if(!el || el.disabled) return;
  const v = el.dataset.v;
  switch(el.dataset.dk){
    case 'air': toggle(v); break;
    case 'clear': clear(); break;
    case 'cam': if(el.dataset.close) S.allCams = false; camera({group: +v}); break;
    case 'allcams': S.allCams = !S.allCams; S.draw(); break;
    case 'fav': toggleFav(v); break;
    case 'sect': S.sect = v; S.allCams = false; S.draw(); break;
    case 'car': camera({car: v}, `Камера: #${v}`); break;
    case 'dpair': deltaPair(v); break;
    case 'macro': runMacro(v); break;
    case 'replay': replayTo(+v); break;
    case 'golive': goLive(); break;
    case 'dset': dirSet({[v]: !(S.dir && S.dir.cfg || {})[v]}); break;
    case 'dmin': dirSet({inc_min: +v}); break;
    case 'devery': dirSet({auto_every: +v}); break;
    case 'dgrp': {
      const g = new Set((S.dir && S.dir.cfg || {}).auto_groups || []);
      if(g.has(v)) g.delete(v); else g.add(v);
      dirSet({auto_groups: [...g]}); break; }
    case 'editor': openEditor(); break;
  }
});
document.addEventListener('input', e => {
  if(e.target.id !== 'dk-q') return;
  S.q = e.target.value; S.draw();
});
document.addEventListener('keydown', e => {
  if(e.target.id !== 'dk-q') return;
  if(e.key === 'Enter'){ e.preventDefault(); pickSearch(); }
  else if(e.key === 'Escape'){ e.preventDefault(); e.stopPropagation(); e.target.value = ''; S.q = ''; e.target.blur(); S.draw(); }
}, true);

const air = () => (S.airActive ? S.airActive() : S.active()) && S.bcOn && !modalOpen();
const deckKey = fn => e => (S.active() && S.bcOn && !modalOpen()) ? fn(e) : false;
const keys = {
  'Escape': e => { if(modalOpen()){ closeEditor(); return; } if(!air()) return false; clear(); },
  '/': deckKey(() => { const q = document.getElementById('dk-q'); if(!q) return false; q.focus(); q.select(); }),
  'L': deckKey(() => { if(!S.dir || (S.dir.live && !S.dir.replay)) return false; goLive(); }),
};
for(let i = 1; i <= 12; i++) keys['F' + i] = e => { if(!air()) return false; const o = S.overlays[i - 1]; if(!o) return false; toggle(o.id); };
for(let i = 1; i <= MAX_FAVS; i++) keys[String(i)] = deckKey(() => {
  const f = favs()[i - 1]; if(!f || !f.g) return false; camera({group: f.g.num});
});
irc.keys(keys);

/* ======================= макросы и их редактор ======================= */

async function runMacro(id){
  const m = S.macros.find(x => x.id === id); if(!m) return;
  const r = await irc.post('/api/broadcast/macro/run', {id});
  if(r.ok){ S.running = id; irc.toast('Макрос: ' + m.title, 'ok'); S.draw(); }
}

const STEP_LABEL = {camera: 'Камера', wait: 'Пауза', overlay: 'Оверлей'};
const ACTIONS = [['show', 'показать'], ['hide', 'убрать'], ['toggle', 'переключить'], ['only', 'только этот']];
let E = null;                        // черновик: копия списка и выбранный макрос
let modal = null;
const modalOpen = () => !!E;

function defaultStep(type){
  if(type === 'wait') return {type: 'wait', sec: 1};
  if(type === 'overlay') return {type: 'overlay', id: (S.overlays[0] || {}).id || 'tower', action: 'show'};
  return {type: 'camera', group: '', car: 'focus'};
}

function stepHtml(st, i){
  let f = '';
  const sel = (k, opts, w) => `<select class="inp text" data-k="${k}" style="width:${w || 'auto'}">${opts}</select>`;
  if(st.type === 'camera'){
    const names = (S.cams.groups || []).map(g => g.name);
    if(st.group && !names.includes(st.group)) names.push(st.group);
    const numMode = st.car && st.car !== 'focus' && st.car !== 'leader';
    f = sel('group', ['<option value="">текущая группа</option>']
          .concat(names.map(g => `<option ${g === st.group ? 'selected' : ''}>${esc(g)}</option>`)).join(''))
      + sel('carmode', `<option value="focus" ${!numMode && st.car !== 'leader' ? 'selected' : ''}>пилот в кадре</option>
          <option value="leader" ${st.car === 'leader' ? 'selected' : ''}>лидер</option>
          <option value="num" ${numMode || st._num ? 'selected' : ''}>номер машины</option>`)
      + (numMode || st._num ? `<input class="inp" data-k="car" value="${esc(numMode ? st.car : '')}" style="width:64px" placeholder="#">` : '');
  }else if(st.type === 'wait'){
    f = `<input class="inp" data-k="sec" type="number" min="0.1" max="120" step="0.1" value="${st.sec ?? 1}" style="width:84px"><span class="dim">сек</span>`;
  }else{
    f = sel('action', ACTIONS.map(([v, t]) => `<option value="${v}" ${st.action === v ? 'selected' : ''}>${t}</option>`).join(''))
      + sel('id', S.overlays.map(o => `<option value="${o.id}" ${o.id === st.id ? 'selected' : ''}>${esc(o.title)}</option>`).join('')
          + `<option value="*" ${st.id === '*' ? 'selected' : ''}>все оверлеи</option>`);
  }
  return `<div class="row step" data-i="${i}" style="padding:8px 0;border-bottom:1px solid var(--line)">
    <span class="num faint" style="width:20px">${i + 1}</span>
    ${sel('type', Object.entries(STEP_LABEL).map(([v, t]) => `<option value="${v}" ${st.type === v ? 'selected' : ''}>${t}</option>`).join(''), '112px')}
    ${f}<span class="grow"></span>
    <button class="k s" data-dm="up" title="выше">↑</button><button class="k s" data-dm="down" title="ниже">↓</button>
    <button class="k s" data-dm="del">Убрать</button></div>`;
}

function drawEditor(){
  const m = E.list[E.sel];
  modal.innerHTML = `<div class="p" style="width:min(860px,calc(100vw - 32px));max-height:calc(100% - 32px)">
    <div class="ph">Макросы пульта<span class="r">шаги выполняются по порядку</span></div>
    <div style="display:grid;grid-template-columns:220px minmax(0,1fr);min-height:0;flex:1;overflow:hidden">
      <div class="scroll" style="border-right:1px solid var(--line)">
        ${E.list.map((x, i) => `<div class="tr click ${i === E.sel ? 'cur' : ''}" data-dm="pick" data-i="${i}" style="grid-template-columns:1fr">
          <span class="bar" style="background:${i === E.sel ? 'var(--acc)' : 'transparent'}"></span><span class="nm">${esc(x.title || 'без названия')}</span></div>`).join('')}
        <div class="pb"><button class="k s full" data-dm="add">+ Новый макрос</button></div></div>
      <div class="pb scroll">${m ? `
        <div class="lbl">Название кнопки</div>
        <input class="inp text" id="dm-title" value="${esc(m.title)}" maxlength="40">
        <div class="lbl">Шаги</div>
        <div>${m.steps.map(stepHtml).join('') || '<div class="dim">Шагов пока нет.</div>'}</div>
        <div class="row"><button class="k s" data-dm="addstep" data-t="camera">+ Камера</button>
          <button class="k s" data-dm="addstep" data-t="wait">+ Пауза</button>
          <button class="k s" data-dm="addstep" data-t="overlay">+ Оверлей</button></div>`
        : '<div class="dim">Выберите макрос слева или создайте новый.</div>'}</div></div>
    <div class="row" style="padding:12px;border-top:1px solid var(--line)">
      ${m ? '<button class="k s hit" data-dm="mdel">Удалить макрос</button>' : ''}
      <span class="grow"></span>
      <button class="k" data-dm="close">Отмена<kbd>Esc</kbd></button>
      <button class="k pri" data-dm="save">Сохранить</button></div></div>`;
}

function openEditor(){
  if(!modal){
    modal = document.createElement('div');
    modal.className = 'gate'; modal.hidden = true;
    modal.style.cssText = 'top:0;bottom:0;z-index:35';
    document.body.appendChild(modal);
    modal.addEventListener('click', onModalClick);
    modal.addEventListener('change', onModalChange);
    modal.addEventListener('input', onModalInput);
  }
  E = {list: JSON.parse(JSON.stringify(S.macros)), sel: S.macros.length ? 0 : -1};
  drawEditor();
  modal.hidden = false;
}
function closeEditor(){ if(modal) modal.hidden = true; E = null; S.draw(); }

async function onModalClick(e){
  if(e.target === modal){ closeEditor(); return; }
  const t = e.target.closest('[data-dm]'); if(!t || !E) return;
  const m = E.list[E.sel];
  switch(t.dataset.dm){
    case 'pick': E.sel = +t.dataset.i; break;
    case 'add': E.list.push({id: '', title: 'Новый макрос', steps: []}); E.sel = E.list.length - 1; break;
    case 'addstep': if(m) m.steps.push(defaultStep(t.dataset.t)); break;
    case 'up': case 'down': case 'del': {
      if(!m) return;
      const i = +t.closest('.step').dataset.i;
      if(t.dataset.dm === 'del') m.steps.splice(i, 1);
      if(t.dataset.dm === 'up' && i > 0) [m.steps[i - 1], m.steps[i]] = [m.steps[i], m.steps[i - 1]];
      if(t.dataset.dm === 'down' && i < m.steps.length - 1) [m.steps[i + 1], m.steps[i]] = [m.steps[i], m.steps[i + 1]];
      break; }
    case 'mdel': if(m){ E.list.splice(E.sel, 1); E.sel = Math.min(E.sel, E.list.length - 1); } break;
    case 'close': closeEditor(); return;
    case 'save': {
      const clean = E.list.map(x => ({id: x.id, title: x.title, steps: x.steps.map(({_num, ...rest}) => rest)}));
      const r = await irc.post('/api/broadcast/macros', {macros: clean});
      if(r.macros){ S.macros = r.macros; closeEditor(); irc.toast('Макросы сохранены', 'ok'); }
      return; }
    default: return;
  }
  drawEditor();
}
function onModalChange(e){
  const el = e.target, row = el.closest('.step');
  if(!row || !E) return;
  const steps = E.list[E.sel].steps, i = +row.dataset.i, st = steps[i];
  const k = el.dataset.k;
  if(k === 'type'){ steps[i] = defaultStep(el.value); drawEditor(); return; }
  if(k === 'carmode'){ st.car = el.value === 'num' ? '' : el.value; st._num = el.value === 'num'; drawEditor(); return; }
  if(k === 'sec') st.sec = +el.value;
  else if(k === 'car') st.car = el.value.trim().replace(/^#/, '');
  else st[k] = el.value;
}
function onModalInput(e){
  if(!E) return;
  if(e.target.id === 'dm-title') E.list[E.sel].title = e.target.value;
  const row = e.target.closest('.step');
  if(row && e.target.dataset.k === 'car') E.list[E.sel].steps[+row.dataset.i].car = e.target.value.trim().replace(/^#/, '');
}

/* ======================= опрос ======================= */

async function pollDir(){
  const d = await irc.get('/api/broadcast/director');
  if(d && d.cfg){ S.dir = d; S.draw(); }
}
async function dirSet(body){
  const r = await irc.post('/api/broadcast/director', body, true);
  if(r && r.cfg){ S.dir = r; S.draw(); }
}
async function replayTo(id){
  const x = ((S.dir || {}).incidents || []).find(i => i.id === id);
  const r = await irc.post('/api/broadcast/replay', {id}, true);
  if(r && r.ok) irc.toast(`Повтор: #${x ? x.car_number : ''}`, 'wa');
  else irc.toast(r && r.error === 'busy' ? 'Шторка ещё идёт' : 'Повтор не запустился', 'er');
  pollDir();
}
async function goLive(){
  const r = await irc.post('/api/broadcast/replay', {live: true}, true);
  if(r && r.ok) irc.toast('Возвращаемся в LIVE');
  pollDir();
}
async function pollCards(){
  const d = await irc.get('/api/broadcast/cards');
  S.cards = d && d.rows ? d : null;
  S.draw();
}
async function pollState(){
  if(!S.bcOn) return;
  const [v, c] = await Promise.all([irc.get('/api/broadcast/visibility'), irc.get('/api/broadcast/cameras')]);
  if(v && v.overlays) S.overlays = v.overlays;
  if(c && c.groups) S.cams = c;
  S.draw();
}
async function loadFavs(){
  const s = await irc.get('/api/broadcast/settings');
  if(s) S.favs = Array.isArray(s.cam_favorites) ? s.cam_favorites : [];
}
async function loadMacros(){
  if(!S.bcOn) return;
  const r = await irc.get('/api/broadcast/macros');
  if(r){ S.macros = r.macros || []; S.running = r.running || null; }
  if(!E) S.draw();
}
window.addEventListener('resize', () => S.draw());
const shown = () => S.active() || (S.airActive && S.airActive());
setInterval(() => { if(S.active() && S.bcOn) pollCards(); }, 600);
setInterval(() => { if(shown()) pollState(); }, 1500);
setInterval(() => { if(S.active()) loadMacros(); }, 2500);
setInterval(() => { if(shown() && S.bcOn) pollDir(); }, 1000);

irc.deck = {
  init(opts){ Object.assign(S, opts || {}); },
  html, toggle, clear,
  // состояние программы: включён ли модуль и что в эфире
  setState(st){
    const bc = (st.modules || []).find(m => m.id === 'broadcast');
    const was = S.bcOn;
    S.bcOn = !!(bc && bc.started);
    if(bc && bc.overlays) S.overlays = bc.overlays;
    if(S.bcOn && !was){ pollState(); loadMacros(); pollCards(); loadFavs(); pollDir(); }
  },
};
})();
