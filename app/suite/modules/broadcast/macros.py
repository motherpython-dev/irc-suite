"""Макросы пульта: именованные кнопки, выполняющие шаги по порядку.

Шаги трёх типов — ровно то, из чего режиссёр собирает сцену:

    camera   группа камер и машина: пилот в кадре, лидер или номер
    wait     пауза в секундах
    overlay  показать, убрать, переключить или оставить в эфире только этот

Пример: «Кокпит + разрыв» — камера в салон машины в кадре, через секунду
выводится оверлей разрыва пары.

Макрос выполняется в своём потоке: пауза не должна подвешивать HTTP-запрос
пульта. Новый запуск прерывает предыдущий — иначе два сценария дрались бы
за камеру.
"""
from __future__ import annotations

import re
import threading
import uuid

OVERLAY_ACTIONS = ("show", "hide", "toggle", "only")
MAX_MACROS = 40
MAX_STEPS = 20
MAX_WAIT = 120.0


def normalize_macros(raw, overlay_ids) -> list[dict]:
    """Привести присланное из редактора к проверенному виду.

    Всё незнакомое отбрасываем молча: редактор может прислать шаг, который
    сервер не умеет выполнять, и лучше потерять его при сохранении, чем
    падать в эфире на нажатии кнопки.
    """
    out, seen = [], set()
    for m in (raw or [])[:MAX_MACROS]:
        if not isinstance(m, dict):
            continue
        title = str(m.get("title") or "").strip()[:40] or "Макрос"
        mid = str(m.get("id") or "").strip()[:48]
        if not mid or mid in seen:
            mid = uuid.uuid4().hex[:8]
        seen.add(mid)
        steps = [s for s in (_step(x, overlay_ids)
                             for x in (m.get("steps") or [])[:MAX_STEPS]) if s]
        out.append({"id": mid, "title": title, "steps": steps})
    return out


def _step(st, overlay_ids) -> dict | None:
    if not isinstance(st, dict):
        return None
    kind = st.get("type")
    if kind == "wait":
        try:
            sec = float(st.get("sec", 1.0))
        except (TypeError, ValueError):
            sec = 1.0
        return {"type": "wait", "sec": round(min(MAX_WAIT, max(0.1, sec)), 1)}
    if kind == "camera":
        car = str(st.get("car") or "focus").strip().lstrip("#")
        # номер храним строкой: у iRacing «07» и «7» — разные машины
        if car not in ("focus", "leader") and not re.fullmatch(r"\d{1,3}", car):
            car = "focus"
        return {"type": "camera", "group": str(st.get("group") or "").strip()[:32],
                "car": car}
    if kind == "overlay":
        oid = str(st.get("id") or "")
        if oid != "*" and oid not in overlay_ids:
            return None
        act = st.get("action") if st.get("action") in OVERLAY_ACTIONS else "show"
        if oid == "*" and act not in ("show", "hide"):
            act = "hide"                     # «только этот» для всех смысла не имеет
        return {"type": "overlay", "id": oid, "action": act}
    return None


class MacroRunner:
    """Выполняет макросы модуля трансляции."""

    def __init__(self, module) -> None:
        self.module = module
        self._cancel = threading.Event()
        self.running: str | None = None

    def run(self, macro_id: str) -> dict:
        macros = self.module.ctx.config.get("macros") or []
        m = next((x for x in macros if x.get("id") == macro_id), None)
        if not m:
            return {"ok": False, "error": "unknown_macro"}
        self.stop()
        cancel = threading.Event()
        self._cancel = cancel
        threading.Thread(target=self._exec, args=(m, cancel), daemon=True,
                         name=f"macro-{macro_id}").start()
        return {"ok": True, "id": macro_id}

    def stop(self) -> None:
        self._cancel.set()

    def _exec(self, m: dict, cancel: threading.Event) -> None:
        log = self.module.ctx.log
        self.running = m["id"]
        self._publish()
        try:
            for st in m.get("steps") or []:
                if cancel.is_set():
                    break
                kind = st.get("type")
                if kind == "wait":
                    cancel.wait(float(st.get("sec") or 0.0))
                elif kind == "camera":
                    res = self.module.camera({"car": st.get("car"),
                                              "group": st.get("group")})
                    if not res.get("ok"):
                        log.info("макрос «%s»: камера не переключилась (%s)",
                                 m["title"], res.get("error", "сим не готов"))
                elif kind == "overlay":
                    self.module.apply_overlay(st.get("id"), st.get("action"))
        except Exception:
            log.exception("макрос «%s» упал", m.get("title"))
        finally:
            # новый запуск уже выставил своё имя — не затираем его
            if self._cancel is cancel:
                self.running = None
            self._publish()

    def _publish(self) -> None:
        try:
            self.module.ctx.publish("macro", {"running": self.running})
        except Exception:
            pass
