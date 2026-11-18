/* Общие помощники интерфейса iRC — подключаются всеми окнами программы.

   Главное здесь — setHTML: обновить кусок страницы новой разметкой, не
   пересоздавая его. Панель получает состояние каждую секунду; раньше она
   собирала правую часть заново, и элемент под курсором успевал смениться
   между нажатием и отпусканием кнопки мыши — клик терялся. setHTML сравнивает
   новую разметку с тем, что уже на странице, и меняет только отличающиеся
   узлы, текст и атрибуты. Элементы остаются теми же объектами.

   Поля ввода: значение, которое ввёл человек или подставил скрипт, не
   затирается, пока в разметке не поменялось само значение по умолчанию,
   и никогда — у поля в фокусе.
*/
(function(){
'use strict';

const FORM = new Set(['INPUT', 'TEXTAREA', 'SELECT']);

function sameNode(a, b){
  if(a.nodeType !== b.nodeType) return false;
  if(a.nodeType !== 1) return true;
  if(a.tagName !== b.tagName) return false;
  // ключ задан — узлы одинаковы, только если ключ совпал
  const ka = a.getAttribute('data-key'), kb = b.getAttribute('data-key');
  return ka === kb;
}

function syncAttrs(el, tpl){
  const focused = el === document.activeElement;
  for(const {name, value} of Array.from(tpl.attributes)){
    if(el.getAttribute(name) === value) continue;
    // в шаблоне сменилось значение поля — подставляем; иначе поле не трогаем
    if(FORM.has(el.tagName) && name === 'value'){
      el.setAttribute('value', value);
      if(!focused) el.value = value;
      continue;
    }
    if(FORM.has(el.tagName) && name === 'checked'){
      el.setAttribute('checked', value);
      if(!focused) el.checked = true;
      continue;
    }
    el.setAttribute(name, value);
  }
  for(const {name} of Array.from(el.attributes)){
    if(tpl.hasAttribute(name)) continue;
    el.removeAttribute(name);
    if(FORM.has(el.tagName) && name === 'checked' && !focused) el.checked = false;
  }
}

function patchChildren(parent, tplParent){
  const tpl = Array.from(tplParent.childNodes);
  let cur = parent.firstChild;
  for(const t of tpl){
    if(!cur){ parent.appendChild(t.cloneNode(true)); continue; }
    if(sameNode(cur, t)){
      patchNode(cur, t);
      cur = cur.nextSibling;
      continue;
    }
    // ключевой узел мог сдвинуться: ищем его дальше среди соседей
    const key = t.nodeType === 1 && t.getAttribute('data-key');
    let found = null;
    if(key !== null && key !== false){
      for(let n = cur.nextSibling; n; n = n.nextSibling){
        if(n.nodeType === 1 && n.getAttribute('data-key') === key && n.tagName === t.tagName){ found = n; break; }
      }
    }
    if(found){
      parent.insertBefore(found, cur);
      patchNode(found, t);
      continue;
    }
    parent.insertBefore(t.cloneNode(true), cur);
  }
  while(cur){ const next = cur.nextSibling; parent.removeChild(cur); cur = next; }
}

function patchNode(el, tpl){
  if(el.nodeType === 3 || el.nodeType === 8){
    if(el.nodeValue !== tpl.nodeValue) el.nodeValue = tpl.nodeValue;
    return;
  }
  syncAttrs(el, tpl);
  // содержимое textarea — это его значение: правим только вне фокуса
  if(el.tagName === 'TEXTAREA'){
    if(el.defaultValue !== tpl.textContent){
      el.defaultValue = tpl.textContent;
      if(el !== document.activeElement) el.value = tpl.textContent;
    }
    return;
  }
  patchChildren(el, tpl);
}

/* Обновить содержимое элемента новой разметкой. Ничего не делает, если
   разметка та же, что в прошлый раз. */
function setHTML(el, html){
  if(!el) return;
  if(el.__ircHTML === html) return;
  el.__ircHTML = html;
  const t = document.createElement('template');
  t.innerHTML = html;
  patchChildren(el, t.content);
}

/* ---------- разметка элементов ---------- */

const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;'}[c]));

/* «ВКЛ | ВЫКЛ» вместо переключателя. key уходит в data-key обработчику. */
function onoff(key, on, disabled){
  const d = disabled ? ' disabled' : '';
  return `<span class="onoff" role="group" data-onoff="${esc(key)}">`
    + `<button type="button" class="y" data-v="1" aria-pressed="${!!on}"${d}>ВКЛ</button>`
    + `<button type="button" class="n" data-v="0" aria-pressed="${!on}"${d}>ВЫКЛ</button></span>`;
}

/* Выбор одного из нескольких: opts — [[значение, подпись], ...] */
function seg(key, opts, cur){
  return `<span class="seg" role="group" data-seg="${esc(key)}">` + opts.map(([v, t]) =>
    `<button type="button" data-v="${esc(v)}" aria-pressed="${String(v) === String(cur)}">${esc(t)}</button>`).join('') + '</span>';
}

/* Шапка панели в две строки, как в мокапе стиля F: сверху мелкая оранжевая
   строка (kicker), под ней заголовок, справа значения [[подпись, значение], ...]. */
function ph(kicker, title, stats){
  const st = (stats || []).filter(x => x && x[1] !== undefined && x[1] !== null && x[1] !== '');
  return `<div class="ph h2"><div class="t">${kicker ? `<span class="ki">${esc(kicker)}</span>` : ''}<span class="h">${esc(title)}</span></div>${
    st.length ? `<div class="stats">${st.map(([a, b, c]) => `<div class="stat"><span>${esc(a)}</span><b${
      c ? ` style="color:${c}"` : ''}>${esc(b)}</b></div>`).join('')}</div>` : ''}</div>`;
}

/* Плашка номера в цветах ливреи (/api/plates, обновляется раз в 5 с) —
   как в Standings: курсив, тень, два цвета — градиентом. */
let PLATES = {};
function plate(num){
  const c = PLATES[String(num)] || [];
  const bg = c.length > 1 ? `linear-gradient(90deg,${c[0]},${c[1]})` : (c[0] || '#5C6370');
  return `<span class="plate" style="background:${bg}"><span>${esc(num)}</span></span>`;
}
async function loadPlates(){
  try{ const r = await get('/api/plates'); if(r && typeof r === 'object' && !Array.isArray(r)) PLATES = r; }catch(e){}
}
setTimeout(loadPlates, 300);
setInterval(() => { if(!document.hidden) loadPlates(); }, 5000);

/* Метка статуса: цветной текст на тёмной подложке */
const pill = (text, color) => `<span class="tag pill" style="color:${color || 'var(--dim)'}">${esc(text)}</span>`;

/* Подписать клавишу: <kbd>Y</kbd> */
const kbd = k => k ? `<kbd>${esc(k)}</kbd>` : '';

/* ---------- уведомления ---------- */

function toast(text, kind){
  let box = document.querySelector('.toasts');
  if(!box){ box = document.createElement('div'); box.className = 'toasts'; box.setAttribute('aria-live', 'polite'); document.body.appendChild(box); }
  const el = document.createElement('div');
  if(kind) el.className = kind;
  el.textContent = text;
  box.appendChild(el);
  setTimeout(() => el.remove(), kind === 'er' ? 5000 : 2800);
}

/* ---------- запросы к программе ---------- */

const ERRORS = {
  sim_offline: 'iRacing не запущен или сессия не активна',
  locked: 'Сначала войдите в аккаунт irchamps.gg',
  not_found: 'Не найдено',
};

/* POST в программу. Отказ показывается уведомлением: команда в сим уходит
   «успешно» всегда, и молчаливый отказ оставил бы человека в неведении. */
async function post(url, body, quiet){
  let res;
  try{
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                                body: JSON.stringify(body || {})});
    res = await r.json();
  }catch(e){
    if(!quiet) toast('Нет связи с программой — она закрыта?', 'er');
    return {ok:false, error:'offline'};
  }
  if(res && res.ok === false && !quiet) toast(res.message || ERRORS[res.error] || 'Команда не выполнена', 'er');
  return res || {};
}

async function get(url){
  try{ return await (await fetch(url)).json(); }catch(e){ return null; }
}

/* ---------- горячие клавиши ----------
   irc.keys({'Y': fn, 'F1': fn, 'ArrowDown': fn}). Не срабатывают, пока
   человек печатает в поле; Ctrl/Alt — только если указаны явно ('Ctrl+S').
   На одну клавишу можно повесить несколько обработчиков (вкладка и окно
   входа): срабатывает первый, который не вернул false. */
const KEYS = {};
function keys(map){
  for(const [k, fn] of Object.entries(map)) (KEYS[k] = KEYS[k] || []).push(fn);
}
document.addEventListener('keydown', e => {
  const t = e.target;
  const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
  const k = e.key.length === 1 ? e.key.toUpperCase() : e.key;
  const name = (e.ctrlKey ? 'Ctrl+' : '') + (e.altKey ? 'Alt+' : '') + (e.shiftKey && k.length > 1 ? 'Shift+' : '') + (k === ' ' ? 'Space' : k);
  const list = KEYS[name];
  if(!list) return;
  if(typing && !e.ctrlKey && name !== 'Escape') return;
  if(e.repeat && name !== 'ArrowUp' && name !== 'ArrowDown') return;   // зажатая клавиша не стреляет очередью
  for(const fn of list){
    if(fn(e) !== false){ e.preventDefault(); return; }
  }
});

/* ---------- «вкл | выкл» и выбор: один обработчик на документ ----------
   irc.on('onoff', (key, on, btn) => ...), irc.on('seg', (key, value, btn) => ...).
   Обработчиков может быть несколько — каждый смотрит на свой key. */
const HANDLERS = {onoff: [], seg: []};
function on(kind, fn){ HANDLERS[kind].push(fn); }
document.addEventListener('click', e => {
  const b = e.target.closest('.onoff button, .seg button');
  if(!b || b.disabled) return;
  if(b.getAttribute('aria-pressed') === 'true') return;         // уже в этом положении
  const box = b.parentElement;
  if(box.dataset.onoff !== undefined) HANDLERS.onoff.forEach(fn => fn(box.dataset.onoff, b.dataset.v === '1', b));
  else if(box.dataset.seg !== undefined) HANDLERS.seg.forEach(fn => fn(box.dataset.seg, b.dataset.v, b));
});

/* ---------- окно программы без системной рамки ----------
   В родном окне iRC (WebView2) системной полосы нет: шапка .top — заголовок
   окна (CSS app-region, тащится средствами Windows), а кнопки свернуть /
   развернуть / закрыть рисуем здесь и зовём окно через window.pywebview.api.
   В браузере и на телефоне pywebview нет — ничего не появляется. */
const WIN_ICONS = {
  min: '<svg width="10" height="10" viewBox="0 0 10 10"><path d="M0 5.5h10" stroke="currentColor"/></svg>',
  max: '<svg width="10" height="10" viewBox="0 0 10 10"><rect x=".5" y=".5" width="9" height="9" fill="none" stroke="currentColor"/></svg>',
  close: '<svg width="10" height="10" viewBox="0 0 10 10"><path d="M0 0l10 10M10 0L0 10" stroke="currentColor"/></svg>',
};
function windowControls(){
  const api = window.pywebview && window.pywebview.api;
  const top = document.querySelector('.top');
  if(!api || !api.toggle_maximize || !top || top.querySelector('.wctl')) return;
  document.documentElement.classList.add('native');
  const box = document.createElement('div');
  box.className = 'wctl';
  box.innerHTML = `<button type="button" data-win="minimize" title="Свернуть">${WIN_ICONS.min}</button>`
    + `<button type="button" data-win="toggle_maximize" title="Развернуть">${WIN_ICONS.max}</button>`
    + `<button type="button" class="x" data-win="close" title="Закрыть окно (программа останется в трее)">${WIN_ICONS.close}</button>`;
  box.addEventListener('click', e => {
    const b = e.target.closest('[data-win]');
    if(b) api[b.dataset.win]();
  });
  top.appendChild(box);
}
window.addEventListener('pywebviewready', windowControls);
if(window.pywebview && window.pywebview.api) windowControls();

/* Режим сима для строки состояния: «■ LIVE · 00:12:03 · ТРАССА» — время
   раньше трассы: в узком окне многоточием срезается конец длинного названия.
   LIVE — красный, REPLAY — жёлтый, OFFLINE — серый; ДЕМО — отдельной меткой. */
function simStatus(st){
  st = st || {};
  const mode = st.mode || 'OFFLINE';
  const cls = mode === 'LIVE' ? 'er' : mode === 'REPLAY' ? 'wa' : '';
  const pad = v => String(v).padStart(2, '0');
  const s = Math.max(0, st.session_time | 0);
  const parts = [mode];
  if(st.connected){
    parts.push(`${pad(s / 3600 | 0)}:${pad(s % 3600 / 60 | 0)}:${pad(s % 60)}${st.session_time_estimated ? ' ≈' : ''}`);
    parts.push((st.track || 'сессия').toUpperCase());
  }
  return `<span class="s ${cls} ell" title="${esc(st.track || '')}">${esc(parts.join(' · '))}</span>`
    + (st.demo ? '<span class="s wa">демо</span>' : '');
}

window.irc = Object.assign(window.irc || {}, {setHTML, esc, onoff, seg, kbd, toast, post, get, keys, on, simStatus, ph, plate, pill});
})();
