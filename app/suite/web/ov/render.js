/* Отрисовщик оверлеев iRC.

   Один на всё: строит оверлей по JSON-описанию (см. modules/broadcast/specs.py)
   и в родном прозрачном окне поверх iRacing, и на странице для OBS.

   Описание: шапка, тело (список / решётка страницами / карточки / одиночный
   блок), подвал. В каждом — элементы на своих координатах: текст, чип,
   плашка, картинка. Текст — шаблон: {поле|формат}, {$.поле} — из корня данных.

   Обновление без перестройки: элементы создаются один раз, дальше меняется
   только текст и состояние. Строки списка стоят по абсолютным координатам,
   поэтому при обгоне строка переезжает на новое место плавно.
*/
(function(){
'use strict';

const OV = document.body.dataset.ov;
const root = document.getElementById('ov');
const PREVIEW = new URLSearchParams(location.search).has('preview');

let spec = null, specVer = null, data = null;
let shown = null, hideTimer = null;     // null — состояние ещё не отправлено
let page = 0, pageAt = Date.now();
let built = null;                   // построенное дерево
let locked = false;                 // редактор подал своё описание — с сервера не перечитываем
let lastSize = '';

// ---------- утилиты ----------

const CLASS = {GT3:'#FF5A1F', PRO:'#FF5A1F', GT4:'#3B82F6', AM:'#3B82F6',
               SILVER:'#94A3B8', GOLD:'#E3B341'};
const FLAG = {green:'#2EA043', yellow:'#E3B341', red:'#DA3633', white:'#E6EDF3',
              checkered:'#E6EDF3', none:'transparent'};
const SLANT = 0.22;

function color(v){
  if(typeof v !== 'string') return v;
  if(v[0] === '$') return (spec.palette || {})[v.slice(1)] || v;
  return v;
}
function font(v){
  if(!v) return (spec.fonts || {}).text;
  return (spec.fonts || {})[v] || v;
}
function get(obj, path){
  return String(path).split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
function surname(n){
  const p = String(n || '').trim().split(/\s+/).filter(Boolean);
  if(!p.length) return '';
  let t = p[p.length - 1];
  if(t.length <= 2 && p.length > 1) t = p[0];
  return t.toUpperCase();
}
function laptime(t){
  if(!t || t <= 0) return '—';
  const m = Math.floor(t / 60);
  return m ? `${m}:${(t - m * 60).toFixed(3).padStart(6, '0')}` : t.toFixed(3);
}

const FMT = {
  surname,
  upper: v => String(v ?? '').toUpperCase(),
  gap: (v, it) => it && it.position === 1 ? 'ЛИДЕР' : (v > 0 ? '+' + v.toFixed(1) : '—'),
  fixed1: v => v == null ? '—' : Number(v).toFixed(1),
  plus3: v => v ? '+' + Number(v).toFixed(3) : '',
  laptime,
  sec1: v => v == null ? '—' : Number(v).toFixed(1) + 'с',
  slash: v => v ? '/' + v : '',
  trend: v => v < 0 ? '▼ СОКРАЩАЕТСЯ' : (v > 0 ? '▲ РАСТЁТ' : '— ДЕРЖИТСЯ'),
  source_label: v => v === 'qualifying' ? 'ПО РЕЗУЛЬТАТАМ КВАЛИФИКАЦИИ'
                                        : 'КВАЛИФИКАЦИИ НЕТ — ТЕКУЩИЙ ПОРЯДОК',
};

function tpl(str, item, rootd){
  return String(str ?? '').replace(/\{([^}|]+)(?:\|([^}]+))?\}/g, (_, path, f) => {
    const fromRoot = path.startsWith('$.');
    const v = get(fromRoot ? rootd : item, fromRoot ? path.slice(2) : path);
    if(f && FMT[f]) return FMT[f](v, item, rootd);
    return v == null ? '' : v;
  });
}

// Условия: «leader», «!in_pit», «$.pages>1», «count>0».
function cond(c, item, rootd, states){
  if(!c) return true;
  let neg = false;
  if(c[0] === '!'){ neg = true; c = c.slice(1); }
  let ok;
  const m = c.match(/^(\$\.)?([\w.]+)\s*(>|<|==)\s*(.+)$/);
  if(m){
    const v = get(m[1] ? rootd : item, m[2]);
    const r = isNaN(+m[4]) ? m[4] : +m[4];
    ok = m[3] === '>' ? v > r : m[3] === '<' ? v < r : v == r;
  }else if(states && c in states){
    ok = states[c];
  }else{
    ok = !!get(c.startsWith('$.') ? rootd : item, c.replace(/^\$\./, ''));
  }
  return neg ? !ok : ok;
}

function itemStates(item, rootd){
  return {
    leader: item.position === 1,
    in_pit: !!(item.on_pit || item.in_pit_stall || item.in_pit),
    focus: !!(rootd.focus_car && item.car_number === rootd.focus_car),
    closing: item.trend < 0,
    growing: item.trend > 0,
  };
}

// Форма: rect, round (radius), pill, slant, slant-right, slant-left.
function shapeCss(el, shape, radius, h){
  el.style.borderRadius = '';
  el.style.clipPath = '';
  const dx = Math.round((h || 20) * SLANT);
  if(shape === 'round') el.style.borderRadius = (radius ?? 6) + 'px';
  else if(shape === 'pill') el.style.borderRadius = '999px';
  else if(shape === 'slant')
    el.style.clipPath = `polygon(${dx}px 0,100% 0,calc(100% - ${dx}px) 100%,0 100%)`;
  else if(shape === 'slant-right')
    el.style.clipPath = `polygon(0 0,100% 0,calc(100% - ${dx}px) 100%,0 100%)`;
  else if(shape === 'slant-left')
    el.style.clipPath = `polygon(${dx}px 0,100% 0,100% 100%,0 100%)`;
}

// ---------- построение ----------

function mk(cls, parent){
  const d = document.createElement('div');
  if(cls) d.className = cls;
  if(parent) parent.appendChild(d);
  return d;
}

function place(el, e){
  el.style.left = (e.x || 0) + 'px';
  el.style.top = (e.y || 0) + 'px';
  el.style.width = (e.w || 0) + 'px';
  el.style.height = (e.h || 0) + 'px';
}

function styleText(el, e){
  el.style.fontFamily = font(e.font);
  el.style.fontSize = (e.size || 14) + 'px';
  el.style.fontWeight = e.weight || 400;
  el.style.fontStyle = e.italic ? 'italic' : 'normal';
  el.style.letterSpacing = (e.spacing || 0) + 'px';
  el.style.textTransform = e.upper ? 'uppercase' : 'none';
  el.style.justifyContent = e.align === 'right' ? 'flex-end'
                          : e.align === 'center' ? 'center' : 'flex-start';
}

function buildElements(container, elements, sect){
  return (elements || []).map(e => {
    const el = mk('el el-' + e.type, container);
    el.dataset.id = e.id || '';
    el.dataset.sect = sect || '';     // редактор находит элемент по разделу и id
    place(el, e);
    if(e.type === 'image'){
      const img = document.createElement('img');
      img.src = e.src || '';
      img.style.objectFit = e.fit || 'contain';
      el.appendChild(img);
    }
    if(e.type === 'text' || e.type === 'chip'){
      styleText(el, e);
      el._txt = mk('t', el);
    }
    if(e.type === 'chip' || e.type === 'box'){
      shapeCss(el, e.shape, e.radius, e.h);
      if(e.border) el.style.boxShadow = `inset 0 0 0 1px ${color(e.border)}`;
    }
    el.style.opacity = e.opacity ?? 1;
    return {el, e, last: {}};
  });
}

function updateElements(list, item, rootd, states){
  for(const n of list){
    const {el, e} = n;
    const vis = cond(e.when, item, rootd, states);
    if(n.last.vis !== vis){ n.last.vis = vis; el.style.display = vis ? '' : 'none'; }
    if(!vis) continue;
    // состояние может переопределить цвет, заливку, размер
    let over = {};
    for(const [k, v] of Object.entries(e.states || {})) if(states && states[k]) over = {...over, ...v};
    const col = color(over.color || e.color || '$fg');
    if(n.last.col !== col){ n.last.col = col; el.style.color = col; }
    if(e.type === 'chip' || e.type === 'box'){
      let fill = over.fill || e.fill;
      if(fill === 'class'){
        const cls = e.fill_from ? get(item, e.fill_from) : item.class_name;
        fill = CLASS[String(cls || '').toUpperCase()] || '#5A6B85';
      }
      fill = color(fill) || 'transparent';
      if(n.last.fill !== fill){ n.last.fill = fill; el.style.background = fill; }
    }
    if(e.type === 'text' || e.type === 'chip'){
      const txt = tpl(e.text, item, rootd);
      if(n.last.txt !== txt){ n.last.txt = txt; el._txt.textContent = txt; }
    }
  }
}

function buildLayer(parent, def, width, cls){
  if(!def) return null;
  const box = mk('layer ' + cls, parent);
  box.style.width = width + 'px';
  box.style.height = (def.height || 0) + 'px';
  const bg = mk('bg', box);
  bg.style.background = color((def.bg || {}).fill) || 'transparent';
  shapeCss(bg, (def.bg || {}).shape, (def.bg || {}).radius, def.height);
  return {box, def, els: buildElements(box, def.elements, cls === 'head' ? 'header' : 'footer')};
}

// Хвост карточек: плашка после них, привязанная к корню данных (подпись
// «в эфире» у комментаторов). Видна по условию when.
function tailOn(){
  const t = spec.body.tail;
  return !!(t && data && cond(t.when, data, data));
}

function bodyWidth(){
  const b = spec.body;
  if(b.type === 'cards'){
    const n = (get(data, b.items) || []).length;
    let w = n * (b.item.width || 200) + Math.max(0, n - 1) * (b.gap || 0);
    if(tailOn()) w += (n ? (b.gap || 0) : 0) + (b.tail.width || 160);
    return Math.max(w, 1);
  }
  return spec.width || 300;
}

// Свои шрифты из описания: {family, src}. Загружаются редактором.
function fontFaces(){
  let st = document.getElementById('ff');
  if(!st){ st = document.createElement('style'); st.id = 'ff'; document.head.appendChild(st); }
  st.textContent = (spec.font_faces || []).map(f =>
    `@font-face{font-family:'${String(f.family).replace(/'/g, '')}';src:url('${f.src}');font-display:block}`
  ).join('\n');
}

function build(){
  fontFaces();
  root.innerHTML = '';
  const width = bodyWidth();
  const frame = mk('frame', root);
  const f = spec.frame || {};
  frame.style.borderRadius = (f.radius || 0) + 'px';
  if(f.shadow) frame.classList.add('shadow');

  const head = buildLayer(frame, spec.header, width, 'head');
  const body = mk('body', frame);
  body.style.width = width + 'px';
  const stripe = f.flag_stripe ? mk('stripe', frame) : null;
  if(stripe) stripe.style.width = f.flag_stripe + 'px';
  const empty = mk('empty', body);
  let tail = null;
  if(spec.body.type === 'cards' && spec.body.tail){
    const t = spec.body.tail, h = spec.body.item.height || 40;
    const box = mk('item tail', body);
    box.style.width = (t.width || 160) + 'px';
    box.style.height = h + 'px';
    const bg = mk('bg', box);
    bg.style.background = color((t.bg || {}).fill) || 'transparent';
    shapeCss(bg, (t.bg || {}).shape, (t.bg || {}).radius, h);
    tail = {box, els: buildElements(box, t.elements, 'tail')};
  }
  const foot = buildLayer(frame, spec.footer, width, 'foot');

  built = {frame, head, body, foot, stripe, empty, width, items: new Map(), single: null, tail};
  if(spec.body.type === 'single'){
    const b = spec.body;
    body.style.height = (b.height || 100) + 'px';
    const bg = mk('bg', body);
    bg.style.background = color((b.bg || {}).fill) || 'transparent';
    built.single = buildElements(body, b.elements, 'body');
  }
}

// ---------- обновление ----------

function pageInfo(items){
  const b = spec.body;
  if(b.type !== 'grid' || !b.page_size) return {list: items, page: 1, pages: 1};
  const pages = Math.max(1, Math.ceil(items.length / b.page_size));
  if(pages > 1 && Date.now() - pageAt >= (b.page_seconds || 15) * 1000){
    page = (page + 1) % pages; pageAt = Date.now();
  }
  page = Math.min(page, pages - 1);
  return {list: items.slice(page * b.page_size, (page + 1) * b.page_size),
          page: page + 1, pages};
}

function render(){
  if(!spec || !data) return;
  const b = spec.body;
  const items = b.items ? (get(data, b.items) || []) : [];
  if(b.type === 'cards' && bodyWidth() !== built.width) build();

  const pi = pageInfo(items);
  const rootd = Object.assign({}, data, {
    count: items.length, page: pi.page, pages: pi.pages,
    page_from: (pi.page - 1) * (b.page_size || 0) + 1,
    page_to: (pi.page - 1) * (b.page_size || 0) + pi.list.length,
  });

  if(built.head) updateElements(built.head.els, rootd, rootd, {});
  if(built.foot) updateElements(built.foot.els, rootd, rootd, {});

  let bodyH = 0;
  const ready = b.ready ? !!get(data, b.ready) : true;
  const isEmpty = b.type === 'single' ? !ready
                : (!pi.list.length && !(b.type === 'cards' && tailOn()));
  built.empty.textContent = isEmpty ? (b.empty || '') : '';
  built.empty.style.display = isEmpty && b.empty ? '' : 'none';

  if(b.type === 'single'){
    built.single.forEach(n => n.el.style.visibility = isEmpty ? 'hidden' : '');
    if(!isEmpty) updateElements(built.single, data, rootd, itemStates(data, rootd));
    bodyH = isEmpty ? 40 : (b.height || 100);
  }else{
    const it = b.item, cols = b.type === 'grid' ? (b.columns || 1) : 1;
    const gap = b.gap || 0;
    const w = b.type === 'cards' ? (it.width || 200)
            : Math.floor((built.width - gap * (cols - 1)) / cols);
    const seen = new Set();
    pi.list.forEach((item, i) => {
      const key = String(get(item, b.key || 'id') ?? i);
      seen.add(key);
      let n = built.items.get(key);
      if(!n){
        const box = mk('item', built.body);
        box.style.width = w + 'px';
        box.style.height = it.height + 'px';
        const bg = mk('bg', box);
        shapeCss(bg, (it.bg || {}).shape, (it.bg || {}).radius, it.height);
        const edge = mk('edge', box);
        n = {box, bg, edge, els: buildElements(box, it.elements, 'item'), last: {}, fresh: true};
        built.items.set(key, n);
      }
      const st = itemStates(item, rootd);
      let over = {};
      for(const [k, v] of Object.entries(it.states || {})) if(st[k]) over = {...over, ...v};
      const alt = (i % 2 === 1) && it.bg && it.bg.alt;
      const fill = color(over.fill || (alt ? it.bg.alt : (it.bg || {}).fill)) || 'transparent';
      if(n.last.fill !== fill){ n.last.fill = fill; n.bg.style.background = fill; }
      const edge = over.edge ? color(over.edge) : '';
      if(n.last.edge !== edge){ n.last.edge = edge; n.edge.style.background = edge || 'transparent'; }

      let x, y;
      if(b.type === 'cards'){ x = i * (w + gap); y = 0; }
      else { x = (i % cols) * (w + gap); y = Math.floor(i / cols) * (it.height + gap); }
      const tr = `translate(${x}px,${y}px)`;
      if(n.last.tr !== tr){
        n.last.tr = tr;
        if(n.fresh){ n.box.style.transition = 'none'; }
        n.box.style.transform = tr;
        if(n.fresh){ n.box.offsetWidth; n.box.style.transition = ''; n.fresh = false; }
      }
      updateElements(n.els, item, rootd, st);
    });
    built.items.forEach((n, key) => {
      if(!seen.has(key)){ n.box.remove(); built.items.delete(key); }
    });
    if(built.tail){
      const on = tailOn();
      built.tail.box.style.display = on ? '' : 'none';
      if(on){
        const n = pi.list.length;
        built.tail.box.style.transform = `translate(${n ? n * (w + gap) : 0}px,0)`;
        updateElements(built.tail.els, rootd, rootd, {});
      }
    }
    const rows = b.type === 'cards' ? ((pi.list.length || tailOn()) ? 1 : 0)
                                    : Math.ceil(pi.list.length / cols);
    bodyH = rows ? rows * it.height + (rows - 1) * gap : 0;
    if(isEmpty && b.empty) bodyH = 40;
  }

  built.body.style.height = bodyH + 'px';
  if(built.stripe){
    const top = built.head ? built.head.def.height : 0;
    built.stripe.style.top = top + 'px';
    built.stripe.style.height = bodyH + 'px';
    built.stripe.style.background = FLAG[data.flag] || FLAG.none;
  }
  report();
}

// ---------- показ и уход ----------

function setVisible(on){
  if(PREVIEW) on = true;
  if(on === shown) return;
  // первое состояние отправляем всегда: окно поначалу показано самим
  // движком, и без явного «спрятать» убранный оверлей висел бы на экране
  const first = shown === null;
  shown = on;
  clearTimeout(hideTimer);
  const api = window.pywebview && window.pywebview.api;
  if(on){
    if(api) api.show();
    // «on» ставим после принудительной раскладки, а не по кадру анимации:
    // в фоновой вкладке и в OBS кадры могут не приходить, и оверлей
    // оставался бы прозрачным
    void root.offsetWidth;
    setTimeout(() => root.classList.add('on'), 20);
  }else{
    root.classList.remove('on');
    // окно прячем после анимации: прозрачное окно ловит клики даже пустое,
    // и спрятанный оверлей не должен мешать мыши
    if(api) hideTimer = setTimeout(() => api.hide(), first ? 0 : 320);
  }
}

// Мост к окну появляется не сразу. Когда он готов — повторяем текущее
// состояние, иначе первое «показать/спрятать» ушло бы в пустоту.
window.addEventListener('pywebviewready', () => {
  const cur = shown;
  shown = null;
  if(cur !== null) setVisible(cur);
});

// Размер окна под содержимое: окно само подстраивается под оверлей.
function report(){
  const r = built.frame.getBoundingClientRect();
  const pad = 14;                      // запас под тень
  const size = `${Math.ceil(r.width) + pad * 2}x${Math.ceil(r.height) + pad * 2}`;
  if(size === lastSize) return;
  lastSize = size;
  const api = window.pywebview && window.pywebview.api;
  if(api) api.resize(Math.ceil(r.width) + pad * 2, Math.ceil(r.height) + pad * 2);
}

// ---------- опрос ----------

async function loadSpec(){
  if(locked) return;
  try{
    const s = await (await fetch('/api/broadcast/spec?id=' + OV)).json();
    if(!s || s.ok === false) return;
    if(s.version === specVer) return;
    spec = s; specVer = s.version;
    root.dataset.enter = spec.enter || 'fade';
    build(); render();
  }catch(e){}
}

async function pull(){
  if(!spec) return;
  try{
    data = await (await fetch(spec.source)).json();
    setVisible(data.visible !== false);
    render();
  }catch(e){}
}

// для редактора: принять описание без сохранения
window.irOverlay = {
  apply(s){
    locked = true;
    spec = s; specVer = s.version; root.dataset.enter = s.enter || 'fade';
    build(); render();
  },
  // раскладка для редактора: где стоит оверлей внутри страницы
  frame(){ return built && built.frame; },
};

loadSpec().then(pull);
setInterval(pull, 400);
setInterval(loadSpec, 2000);
})();
