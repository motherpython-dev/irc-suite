/* Отрисовщик оверлеев iRC.

   Строит оверлей по JSON-описанию (см. modules/broadcast/specs.py) на
   странице для OBS и в предпросмотре редактора. Окна поверх iRacing рисует
   его двойник core/ui/ov_paint.py по тому же описанию: меняешь одно — сверяй
   с другим.

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
let shown = null;                   // null — состояние ещё не применено
let page = 0, pageAt = Date.now();
let built = null;                   // построенное дерево
let locked = false;                 // редактор подал своё описание — с сервера не перечитываем
let waveId = null;                  // последний показанный выброс флага

// ---------- утилиты ----------

const CLASS = {GT3:'#FF5A1F', PRO:'#FF5A1F', GT4:'#3B82F6', AM:'#3B82F6',
               SILVER:'#94A3B8', GOLD:'#E3B341'};
const FLAG = {green:'#2EA043', yellow:'#E3B341', red:'#DA3633', white:'#E6EDF3',
              checkered:'#E6EDF3', none:'transparent'};
// цвет флага в кромке, плашке и волне — ключ палитры
const TONE = {yellow:'$yellow', green:'$live'};
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
// отрыв: «ЛИДЕР», «+1 КРУГ», «+1:02.345», «+0.412»
function gap3(v, it){
  if(it && it.qual){                            // практика, квалификация: время лучшего, у остальных — отставание
    if(!(it.best_lap > 0)) return '—';
    return it.position === 1 ? laptime(it.best_lap) : '+' + Number(v || 0).toFixed(3);
  }
  if(it && it.position === 1) return 'ЛИДЕР';
  const ld = it && it.laps_down;
  if(ld > 0) return `+${ld} ${ld === 1 ? 'КРУГ' : ld < 5 ? 'КРУГА' : 'КРУГОВ'}`;
  if(!(v > 0)) return '—';
  const m = Math.floor(v / 60);
  return '+' + (m ? `${m}:${(v - m * 60).toFixed(3).padStart(6, '0')}` : v.toFixed(3));
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
  gap3,
  make: v => String(v ?? '').trim().split(/\s+/)[0].split('-')[0].toUpperCase(),     // «Mercedes-AMG GT3» -> MERCEDES
  fixed1: v => v == null ? '—' : Number(v).toFixed(1),
  fixed3: v => v == null ? '—' : Number(v).toFixed(3),
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
    focus: !!(item.focus || (rootd.focus_car && item.car_number === rootd.focus_car)),
    closing: item.trend < 0,
    growing: item.trend > 0,
    gained: item.moved === 'up',
    lost: item.moved === 'down',
    fastest: !!item.fastest,
  };
}

// состояния шапки и подвала — по флагу: заголовок желтеет или зеленеет
function rootStates(d){
  const tone = d && d.flag_tone;
  return {yellow: tone === 'yellow', green: tone === 'green'};
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

function buildElements(container, elements, sect, ch){
  return (elements || []).map(e => {
    const el = mk('el el-' + e.type, container);
    el.dataset.id = e.id || '';
    el.dataset.sect = sect || '';     // редактор находит элемент по разделу и id
    place(el, e);
    // fill_h: высота — по высоте раздела за вычетом отступа, ширина — по
    // пропорции (aspect). Так логотип растёт вместе с шапкой в редакторе.
    if(e.fill_h && ch){
      const pad = e.pad ?? 0, hh = Math.max(1, ch - 2 * pad);
      el.style.top = pad + 'px';
      el.style.height = hh + 'px';
      if(e.aspect) el.style.width = Math.round(hh * e.aspect) + 'px';
    }
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
    let col = over.color || e.color || '$fg';
    if(typeof col === 'string' && col[0] === '@') col = get(item, col.slice(1)) || '#FFFFFF';   // из данных строки
    col = color(col);
    if(n.last.col !== col){ n.last.col = col; el.style.color = col; }
    if(e.type === 'chip' || e.type === 'box'){
      let fill = over.fill || e.fill;
      if(fill === 'class'){
        const cls = e.fill_from ? get(item, e.fill_from) : item.class_name;
        fill = CLASS[String(cls || '').toUpperCase()] || '#5A6B85';
      }else if(typeof fill === 'string' && fill[0] === '@'){
        fill = get(item, fill.slice(1)) || e.fill_default || '#5A6B85';
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

// #RRGGBB + прозрачность -> rgba()
function alpha(c, a){
  const m = /^#([0-9a-f]{6})/i.exec(String(c || ''));
  if(!m) return c;
  const n = parseInt(m[1], 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
}

function buildLayer(parent, def, width, cls){
  if(!def) return null;
  const box = mk('layer ' + cls, parent);
  box.style.width = width + 'px';
  box.style.height = (def.height || 0) + 'px';
  const bg = mk('bg', box);
  bg.style.background = color((def.bg || {}).fill) || 'transparent';
  shapeCss(bg, (def.bg || {}).shape, (def.bg || {}).radius, def.height);
  return {box, def, els: buildElements(box, def.elements, cls === 'head' ? 'header' : 'footer',
                                       def.height)};
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
  if(f.surface){
    // поверхность: цвет панели и оранжевое свечение из левого верхнего угла
    const s = f.surface;
    frame.style.background = (s.glow ? `radial-gradient(120% 70% at 0 0,${alpha(color(s.glow), .16)},transparent 60%),` : '')
                           + (color(s.fill) || 'transparent');
  }

  const head = buildLayer(frame, spec.header, width, 'head');
  const flagbar = spec.flagbar ? mk('flagbar', frame) : null;
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
    tail = {box, els: buildElements(box, t.elements, 'tail', h)};
  }
  const foot = buildLayer(frame, spec.footer, width, 'foot');

  const sep = spec.body.sep ? mk('sep', body) : null;
  built = {frame, head, body, foot, stripe, empty, width, items: new Map(), single: null, tail, flagbar, sep};
  if(spec.body.type === 'single'){
    const b = spec.body;
    body.style.height = (b.height || 100) + 'px';
    const bg = mk('bg', body);
    bg.style.background = color((b.bg || {}).fill) || 'transparent';
    built.single = buildElements(body, b.elements, 'body', b.height || 100);
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

  const rs = rootStates(data);
  if(built.head) updateElements(built.head.els, rootd, rootd, rs);
  if(built.foot) updateElements(built.foot.els, rootd, rootd, rs);
  surface();
  flagBar();

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
    const pad = b.pad || 0;
    // «топ + окно»: после sep_after строк — разделитель с подписью
    const sepAfter = b.sep ? (+get(data, String(b.sep.after || '').replace(/^\$\./, '')) || 0) : 0;
    const sepH = sepAfter > 0 && sepAfter < pi.list.length ? (b.sep.height || 22) : 0;
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
        n = {box, bg, edge, els: buildElements(box, it.elements, 'item', it.height),
             last: {}, fresh: true};
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
      else { x = (i % cols) * (w + gap); y = pad + Math.floor(i / cols) * (it.height + gap) + (sepH && i >= sepAfter ? sepH : 0); }
      n.y = y;
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
      const on = tailOn(), tl = built.tail;
      tl.box.style.display = on ? '' : 'none';
      if(on){
        const n = pi.list.length;
        tl.x = n ? n * (w + gap) : 0;
        const tr = `translate(${tl.x}px,0)`;
        if(!tl.on){
          // первая расстановка — сразу на место, дальше подпись выезжает сама
          tl.box.style.transition = 'none';
          tl.box.style.transform = tr;
          void tl.box.offsetWidth;
          tl.box.style.transition = '';
          tailEnter();
        }else{
          tl.box.style.transform = tr;           // число карточек сменилось — переезд
        }
        updateElements(tl.els, rootd, rootd, {});
      }
      tl.on = on;
    }
    const rows = b.type === 'cards' ? ((pi.list.length || tailOn()) ? 1 : 0)
                                    : Math.ceil(pi.list.length / cols);
    bodyH = rows ? rows * it.height + (rows - 1) * gap + 2 * pad + sepH : 0;
    if(isEmpty && b.empty) bodyH = 40;
    if(built.sep){
      built.sep.style.display = sepH ? 'flex' : 'none';
      if(sepH){
        const s = b.sep;
        built.sep.style.left = (s.x ?? 12) + 'px';
        built.sep.style.right = (s.x ?? 12) + 'px';
        built.sep.style.top = (pad + sepAfter * (it.height + gap) - gap) + 'px';
        built.sep.style.height = sepH + 'px';
        built.sep.style.color = color(s.color || '$dim');
        const txt = tpl(s.text, rootd, rootd);
        if(built.sep._t !== txt){ built.sep._t = txt; built.sep.innerHTML = '<span></span>'; built.sep.firstChild.textContent = txt; }
      }
    }
  }

  built.body.style.height = bodyH + 'px';
  wave();
  if(built.stripe){
    const top = built.head ? built.head.def.height : 0;
    built.stripe.style.top = top + 'px';
    built.stripe.style.height = bodyH + 'px';
    built.stripe.style.background = FLAG[data.flag] || FLAG.none;
  }
}

// ---------- поверхность, флаг, волна ----------

// кромка слева и рамка: оранжевые, под жёлтым флагом — жёлтые, на рестарте — зелёные
function surface(){
  const s = (spec.frame || {}).surface;
  if(!s) return;
  const tone = TONE[data.flag_tone];
  const edge = color(tone || s.edge_color || '$accent');
  const glow = alpha(edge, .14);
  const sh = [];
  if(s.edge) sh.push(`inset ${s.edge}px 0 0 ${edge}`);
  if(s.border) sh.push(`inset 0 0 0 1px ${color(s.border)}`);
  sh.push(`0 0 18px ${glow}`);
  if((spec.frame || {}).shadow) sh.push('0 8px 22px -8px rgba(0,0,0,.7)');
  const v = sh.join(',');
  if(built.frame._sh !== v){ built.frame._sh = v; built.frame.style.boxShadow = v; }
}

function flagBar(){
  const fb = built.flagbar;
  if(!fb) return;
  const d = spec.flagbar, bar = data.flag_bar;
  if(!bar || !bar.text){ fb.style.display = 'none'; return; }
  fb.style.display = 'flex';
  const m = d.margin ?? 6;
  fb.style.margin = `${m}px ${m}px 0`;
  fb.style.height = (d.height || 26) + 'px';
  fb.style.borderRadius = (d.radius ?? 4) + 'px';
  fb.style.background = color(TONE[bar.tone] || '$yellow');
  if(fb._t !== bar.text){ fb._t = bar.text; fb.textContent = bar.text; }
}

// Выброс флага: одна волна сверху вниз — полоса света и вспышка шапки и
// строк в момент её прохода. Длительность и форма — как в ov_paint.py.
const WAVE_MS = 1100, LIT_MS = 550;
function wave(){
  const ev = data.flag_event;
  if(!ev || !(spec.frame || {}).wave || ev.id === waveId) return;
  waveId = ev.id;
  if(!(ev.age >= 0 && ev.age < 1.5)) return;          // открыли оверлей позже — не догоняем
  const fr = built.frame, H = fr.offsetHeight;
  if(!H || !fr.animate) return;
  const col = color(TONE[ev.tone] || '$yellow'), lit = alpha(col, .88);
  const lag = ev.age * 1000;
  let w = fr.querySelector(':scope > .wave');
  if(!w){ w = mk('wave', fr); mk('band', w); }
  const band = w.firstChild;
  band.style.background = `linear-gradient(180deg,transparent,${lit} 50%,transparent)`;
  band.animate([{opacity:.9, transform:'translateY(-45px)'}, {opacity:.9, offset:.94},
                {opacity:0, transform:`translateY(${H - 45}px)`}],
               {duration: WAVE_MS, delay: -lag, easing: 'linear', fill: 'both'});
  const flash = (box, yc) => {
    let l = box.querySelector(':scope > .lit');
    if(!l) l = mk('lit', box);
    l.style.background = lit;
    l.style.boxShadow = `0 0 18px ${lit}`;
    const dl = Math.max(0, yc / H * WAVE_MS - 120) - lag;
    l.animate([{opacity:0}, {opacity:1, offset:.3}, {opacity:0}],
              {duration: LIT_MS, delay: dl, easing: 'linear', fill: 'both'});
  };
  const headH = built.head ? built.head.def.height || 0 : 0;
  if(built.head) flash(built.head.box, headH / 2);
  const top = headH + (built.flagbar && built.flagbar.style.display !== 'none' ? built.flagbar.offsetHeight + 6 : 0);
  const ih = (spec.body.item || {}).height || 0;
  built.items.forEach(n => flash(n.box, top + (n.y || 0) + ih / 2));
}

// Подпись после карточек («в эфире») выезжает отдельно, вслед за плашкой:
// при появлении оверлея и когда подпись включили. Как tail_enter в ov_paint.py.
function tailEnter(){
  const tl = built && built.tail, en = spec.body.tail && spec.body.tail.enter;
  if(!tl || !en || !tl.box.animate) return;
  const dx = (en.from === 'left' ? -1 : 1) * (en.shift ?? 60);
  tl.box.animate([{transform: `translate(${tl.x + dx}px,0)`, opacity: 0},
                  {transform: `translate(${tl.x}px,0)`, opacity: 1}],
                 {duration: (en.duration ?? .45) * 1000, delay: (en.delay ?? .22) * 1000,
                  easing: 'cubic-bezier(.2,.8,.2,1)', fill: 'backwards'});
}

// ---------- показ и уход ----------

function setVisible(on){
  if(PREVIEW) on = true;
  if(on === shown) return;
  shown = on;
  if(on){
    // «on» ставим после принудительной раскладки, а не по кадру анимации:
    // в фоновой вкладке и в OBS кадры могут не приходить, и оверлей
    // оставался бы прозрачным
    void root.offsetWidth;
    setTimeout(() => root.classList.add('on'), 20);
    if(built && built.tail && built.tail.on) tailEnter();      // снова в эфире — подпись выезжает заново
  }else{
    root.classList.remove('on');
  }
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
