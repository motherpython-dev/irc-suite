/* Сообщить об ошибке — кнопка в шапке панели и модальное окно.
   Стиль — те же переменные панели, что и у сайта (--surface, --accent…).
   Отправляет ядро (core/bugreport.py): оно добавит версию, систему, модули,
   состояние iRacing и журнал и передаст отчёт на irchamps.gg. */
(function(){
  var MAX = 3, MAX_BYTES = 5 * 1024 * 1024;

  // Иконки — вектор, цвет берут из currentColor (фирменный --accent)
  function svg(paths, size){
    return '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size + '" fill="none" stroke="currentColor" '
      + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="display:block">'
      + paths + '</svg>';
  }
  var BUG = '<path d="m8 2 1.88 1.88"/><path d="M14.12 3.88 16 2"/><path d="M9 7.13v-1a3 3 0 1 1 6 0v1"/>'
    + '<path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6"/><path d="M12 20v-9"/>'
    + '<path d="M6.53 9C4.6 8.8 3 7.1 3 5"/><path d="M6 13H2"/><path d="M3 21c0-2.1 1.7-3.9 3.8-4"/>'
    + '<path d="M20.97 5c0 2.1-1.6 3.8-3.5 4"/><path d="M22 13h-4"/><path d="M17.2 17c2.1.1 3.8 1.9 3.8 4"/>';
  var CAMERA = '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/>'
    + '<circle cx="12" cy="13" r="3"/>';
  var CHECK = '<path d="M20 6 9 17l-5-5"/>';
  var CROSS = '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>';

  var css = `
  .bgr-hbtn{width:32px;height:32px;padding:0;border-radius:50%;display:grid;place-items:center;cursor:pointer;
            background:var(--surface-2);border:1px solid var(--line);color:var(--accent);line-height:0;
            margin-right:6px;flex:none}
  .bgr-hbtn:hover{background:rgba(255,90,31,.12);border-color:var(--accent)}
  .bgr-ov{position:fixed;inset:0;z-index:100;display:none;align-items:center;justify-content:center;padding:16px;
          background:rgba(5,7,12,.66);backdrop-filter:blur(3px)}
  .bgr-ov.open{display:flex}
  .bgr-box{width:100%;max-width:540px;max-height:calc(100vh - 32px);overflow-y:auto;border-radius:18px;
           background:var(--surface);border:1px solid var(--line);color:var(--fg);box-shadow:var(--shadow);padding:22px 22px 18px}
  .bgr-h{font-family:'Rubik','Inter',sans-serif;font-weight:700;font-size:18px;display:flex;align-items:center;gap:9px}
  .bgr-ic{width:32px;height:32px;border-radius:10px;display:inline-grid;place-items:center;flex:none;
          background:rgba(255,90,31,.16);color:var(--accent);line-height:0}
  .bgr-sub{color:var(--dim);font-size:13px;margin:6px 0 16px;line-height:1.45}
  .bgr-lbl{display:block;font-size:12px;font-weight:600;color:var(--dim);margin:0 0 5px}
  .bgr-fld{width:100%;box-sizing:border-box;padding:9px 11px;border-radius:10px;font-size:14px;color:inherit;
           font-family:inherit;background:var(--surface-2);border:1px solid var(--line);resize:vertical}
  .bgr-fld:focus{outline:none;border-color:var(--accent)}
  .bgr-row{margin-bottom:13px}
  .bgr-drop{border:1.5px dashed var(--line-strong);border-radius:12px;padding:12px;text-align:center;
            font-size:13px;color:var(--dim);cursor:pointer}
  .bgr-drop:hover,.bgr-drop.drag{border-color:var(--accent);background:rgba(255,90,31,.08)}
  .bgr-snap svg{color:var(--accent)}
  .bgr-snap{margin-top:8px;display:inline-flex;align-items:center;gap:7px;font-size:13px;padding:7px 12px;border-radius:10px;
            background:var(--surface-2);border:1px solid var(--line);color:var(--fg);cursor:pointer}
  .bgr-snap:hover{border-color:var(--line-strong)}
  .bgr-thumbs{display:flex;flex-wrap:wrap;gap:8px;margin-top:9px}
  .bgr-th{position:relative;width:88px;height:58px;border-radius:9px;overflow:hidden;border:1px solid var(--line)}
  .bgr-th img{width:100%;height:100%;object-fit:cover;display:block}
  .bgr-th button{position:absolute;top:3px;right:3px;width:20px;height:20px;border-radius:6px;border:0;padding:0;
                 background:rgba(0,0,0,.65);color:#fff;cursor:pointer;display:grid;place-items:center;line-height:0}
  .bgr-chk{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--dim)}
  .bgr-chk input{width:16px;height:16px;accent-color:var(--accent)}
  .bgr-err{color:#EF4444;font-size:13px;min-height:18px;margin-top:8px}
  .bgr-btns{display:flex;justify-content:flex-end;gap:8px;margin-top:8px}
  .bgr-ok{text-align:center;padding:18px 8px}
  .bgr-ok .bgr-ic{width:48px;height:48px;font-size:20px;background:rgba(34,197,94,.14);color:#22C55E;margin-bottom:10px}`;
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  var ov = document.createElement('div');
  ov.className = 'bgr-ov'; ov.setAttribute('role', 'dialog'); ov.setAttribute('aria-modal', 'true');
  ov.innerHTML = `
  <div class="bgr-box">
    <form id="bgrForm" novalidate>
      <div class="bgr-h"><span class="bgr-ic">${svg(BUG, 17)}</span>Сообщить об ошибке</div>
      <p class="bgr-sub">Опишите, что пошло не так. Вместе с отчётом отправим версию программы, систему,
        включённые модули и журнал — это поможет разобраться быстрее.</p>
      <div class="bgr-row"><label class="bgr-lbl" for="bgrDesc">Что случилось</label>
        <textarea class="bgr-fld" id="bgrDesc" rows="4" maxlength="5000"
                  placeholder="Например: оверлей таблицы пропадает после рестарта сессии"></textarea></div>
      <div class="bgr-row"><label class="bgr-lbl" for="bgrSteps">Как повторить (необязательно)</label>
        <textarea class="bgr-fld" id="bgrSteps" rows="2" maxlength="5000" placeholder="Шаги по порядку"></textarea></div>
      <div class="bgr-row"><label class="bgr-lbl">Скриншоты — до 3 штук</label>
        <div class="bgr-drop" id="bgrDrop" tabindex="0">Перетащите сюда, вставьте Ctrl+V или нажмите, чтобы выбрать
          <input type="file" id="bgrFile" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden></div>
        <button type="button" class="bgr-snap" id="bgrSnap">${svg(CAMERA, 15)}Снять экран</button>
        <div class="bgr-thumbs" id="bgrThumbs"></div></div>
      <div class="bgr-row"><label class="bgr-lbl" for="bgrContact">Как с вами связаться (необязательно)</label>
        <input class="bgr-fld" id="bgrContact" maxlength="200" placeholder="Почта или Discord — по умолчанию из аккаунта"></div>
      <label class="bgr-chk"><input type="checkbox" id="bgrLog" checked>Приложить журнал программы</label>
      <div class="bgr-err" id="bgrErr"></div>
      <div class="bgr-btns">
        <button type="button" id="bgrCancel">Отмена</button>
        <button type="submit" class="primary" id="bgrSend">Отправить</button>
      </div>
    </form>
    <div class="bgr-ok" id="bgrOk" hidden>
      <span class="bgr-ic">${svg(CHECK, 22)}</span>
      <div class="bgr-h" style="justify-content:center">Спасибо!</div>
      <p class="bgr-sub" id="bgrOkText"></p>
      <button type="button" id="bgrClose">Закрыть</button>
    </div>
  </div>`;
  document.body.appendChild(ov);

  // Кнопка в шапке — слева от аккаунта
  var hbtn = document.createElement('button');
  hbtn.type = 'button'; hbtn.className = 'bgr-hbtn'; hbtn.title = 'Сообщить об ошибке';
  hbtn.setAttribute('aria-label', 'Сообщить об ошибке'); hbtn.innerHTML = svg(BUG, 16);
  var acct = document.getElementById('acct');
  if (acct && acct.parentNode) acct.parentNode.insertBefore(hbtn, acct);
  else (document.querySelector('header') || document.body).appendChild(hbtn);

  var $ = function(id){ return document.getElementById(id); };
  var shots = [];   // data:-URL картинок

  function render(){
    var box = $('bgrThumbs'); box.innerHTML = '';
    shots.forEach(function(src, i){
      var d = document.createElement('div'); d.className = 'bgr-th';
      d.innerHTML = '<img alt=""><button type="button">' + svg(CROSS, 12) + '</button>';
      d.querySelector('img').src = src;
      d.querySelector('button').onclick = function(){ shots.splice(i, 1); render(); };
      box.appendChild(d);
    });
  }
  function addFiles(list){
    $('bgrErr').textContent = '';
    Array.prototype.forEach.call(list || [], function(f){
      if (!/^image\/(png|jpeg|webp|gif)$/.test(f.type)) { $('bgrErr').textContent = 'Подойдут только картинки PNG, JPG, WebP или GIF.'; return; }
      if (f.size > MAX_BYTES) { $('bgrErr').textContent = 'Скриншот больше 5 МБ.'; return; }
      if (shots.length >= MAX) { $('bgrErr').textContent = 'Не больше 3 скриншотов.'; return; }
      var r = new FileReader();
      r.onload = function(){ if (shots.length < MAX) { shots.push(r.result); render(); } };
      r.readAsDataURL(f);
    });
  }
  function open(){
    $('bgrForm').hidden = false; $('bgrOk').hidden = true; $('bgrErr').textContent = '';
    ov.classList.add('open'); setTimeout(function(){ $('bgrDesc').focus(); }, 30);
  }
  function close(){ ov.classList.remove('open'); }
  window.openBugReport = open;

  hbtn.onclick = open;
  $('bgrCancel').onclick = close; $('bgrClose').onclick = close;
  ov.addEventListener('click', function(e){ if (e.target === ov) close(); });
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && ov.classList.contains('open')) close(); });
  var drop = $('bgrDrop'), fileIn = $('bgrFile');
  drop.onclick = function(){ fileIn.click(); };
  fileIn.onchange = function(){ addFiles(fileIn.files); fileIn.value = ''; };
  ['dragenter', 'dragover'].forEach(function(t){ drop.addEventListener(t, function(e){ e.preventDefault(); drop.classList.add('drag'); }); });
  ['dragleave', 'drop'].forEach(function(t){ drop.addEventListener(t, function(e){ e.preventDefault(); drop.classList.remove('drag'); }); });
  drop.addEventListener('drop', function(e){ addFiles(e.dataTransfer.files); });
  document.addEventListener('paste', function(e){
    if (!ov.classList.contains('open')) return;
    var files = (e.clipboardData && e.clipboardData.files) || [];
    if (files.length) { e.preventDefault(); addFiles(files); }
  });

  // Снимок экрана делает ядро; окно на это время прячем, чтобы в кадр попал iRacing
  $('bgrSnap').onclick = function(){
    if (shots.length >= MAX) { $('bgrErr').textContent = 'Не больше 3 скриншотов.'; return; }
    ov.style.visibility = 'hidden';
    setTimeout(function(){
      fetch('/api/bugreport/snap', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})
        .then(function(r){ return r.json(); })
        .then(function(d){
          ov.style.visibility = '';
          if (d.ok) { shots.push(d.data); render(); } else { $('bgrErr').textContent = d.message || 'Не удалось снять экран.'; }
        })
        .catch(function(){ ov.style.visibility = ''; $('bgrErr').textContent = 'Не удалось снять экран.'; });
    }, 250);
  };

  $('bgrForm').addEventListener('submit', function(e){
    e.preventDefault();
    var desc = $('bgrDesc').value.trim();
    if (desc.length < 5) { $('bgrErr').textContent = 'Опишите ошибку хотя бы парой слов.'; return; }
    var active = document.querySelector('.tab[aria-current="page"], .navitem.active');
    var send = $('bgrSend'); send.disabled = true; $('bgrErr').textContent = '';
    fetch('/api/bugreport', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
      description: desc, steps: $('bgrSteps').value.trim(), contact: $('bgrContact').value.trim(),
      screen: active ? active.textContent.trim() : '', attach_log: $('bgrLog').checked, screenshots: shots
    })})
      .then(function(r){ return r.json(); })
      .then(function(d){
        send.disabled = false;
        if (!d.ok) { $('bgrErr').textContent = d.message || 'Отчёт не отправлен.'; return; }
        $('bgrForm').reset(); shots = []; render();
        $('bgrForm').hidden = true; $('bgrOk').hidden = false; $('bgrOkText').textContent = d.message || '';
      })
      .catch(function(){ send.disabled = false; $('bgrErr').textContent = 'Программа не ответила — попробуйте ещё раз.'; });
  });
})();
