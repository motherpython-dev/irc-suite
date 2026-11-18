"""Сообщить об ошибке из программы.

Окно в панели (web/bugreport.js) собирает описание и скриншоты, ядро
добавляет то, чего человек сам не напишет: версию программы, систему,
включённые модули, состояние iRacing и хвост журнала — и отправляет всё
на сайт (/api/desktop/bugreport) под ключом установки. Сайт сохраняет
отчёт, пишет письмо в поддержку и показывает его в админке.

Скриншот можно снять прямо отсюда (snapshot): окно панели прячется, ядро
фотографирует экраны — так в отчёт попадает сам iRacing, а не панель.
"""
from __future__ import annotations

import base64
import io
import logging
import platform

from . import config
from .version import VERSION

log = logging.getLogger("bugreport")

MAX_SHOTS = 3
LOG_TAIL_LINES = 400
SNAP_MAX_WIDTH = 2560


class BugReporter:
    def __init__(self, app):
        self.app = app

    # ---------- снимок экрана ----------

    def snapshot(self) -> dict:
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab(all_screens=True)
        except Exception as exc:                 # noqa: BLE001
            log.warning("снимок экрана не удался: %s", exc)
            return {"ok": False, "message": "Не удалось снять экран — приложите картинку файлом."}
        if img.width > SNAP_MAX_WIDTH:
            img = img.resize((SNAP_MAX_WIDTH, int(img.height * SNAP_MAX_WIDTH / img.width)))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=82)
        return {"ok": True, "data": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")}

    # ---------- отправка ----------

    def _log_tail(self) -> str:
        try:
            lines = config.log_path().read_text("utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-LOG_TAIL_LINES:])

    def _modules(self) -> str:
        try:
            items = self.app.registry.statuses()
        except Exception:                        # noqa: BLE001
            return ""
        out = []
        for m in items:
            name = m.get("title") or m.get("id")
            state = "вкл" if m.get("started") or m.get("enabled") else "выкл"
            out.append(f"{name}: {state}{' (' + m['error'] + ')' if m.get('error') else ''}")
        return ", ".join(out)

    def _sim(self) -> str:
        try:
            s = self.app.sim.snapshot()
        except Exception:                        # noqa: BLE001
            return ""
        if not s.connected:
            return "не подключён"
        return f"{s.mode}, трасса: {s.track or '—'}, сессия {s.subsession_id or '—'}"

    def send(self, body: dict) -> dict:
        desc = str(body.get("description") or "").strip()
        if len(desc) < 5:
            return {"ok": False, "message": "Опишите ошибку хотя бы парой слов."}
        key = (self.app.license.state or {}).get("key") or ""
        if not key:
            return {"ok": False, "message": "Программа не подключена к аккаунту irchamps.gg — "
                                            "войдите через меню аккаунта и отправьте ещё раз."}
        payload = {
            "description": desc[:5000],
            "steps": str(body.get("steps") or "")[:5000],
            "contact": str(body.get("contact") or "")[:200],
            "screen": str(body.get("screen") or "")[:120],
            "app_version": VERSION,
            "os": f"{platform.system()} {platform.release()} ({platform.version()})",
            "modules": self._modules(),
            "sim": self._sim(),
            "log": self._log_tail() if body.get("attach_log", True) else "",
            "screenshots": [str(s) for s in (body.get("screenshots") or [])][:MAX_SHOTS],
        }
        res = self.app.license._post("/api/desktop/bugreport", payload, key=key)
        if res.get("ok"):
            log.info("баг-репорт отправлен: #%s", res.get("id"))
            return {"ok": True, "id": res.get("id"),
                    "message": f"Отчёт №{res.get('id')} отправлен команде iRChamps. Спасибо!"}
        if res.get("error") == "offline":
            return {"ok": False, "message": "Сайт недоступен — проверьте интернет и попробуйте ещё раз."}
        messages = {"bug.err_big": "Скриншот больше 5 МБ.",
                    "bug.err_type": "Подойдут только картинки PNG, JPG, WebP или GIF.",
                    "bug.err_many": "Не больше 3 скриншотов.",
                    "bug.err_short": "Опишите ошибку хотя бы парой слов."}
        return {"ok": False, "message": messages.get(res.get("error"), "Отчёт не принят — попробуйте ещё раз.")}
