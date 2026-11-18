"""Связь рейс-контроля с сайтом irchamps.gg: какой этап судим и отправка решений.

Организатор заводит этапы заранее, номер сессии iRacing известен только в
день гонки. Как только он появился в симе, программа сообщает сайту номер,
трассу и лигу — сайт сам находит этап из расписания. Если подходящих этапов
несколько или ни одного, сайт отдаёт список, и маршал выбирает в рейс-контроле.

Решения маршала по инцидентам сначала пишутся в локальную базу, потом уходят
на сайт. Нет связи или этап ещё не определён — решения ждут и уходят позже:
рейс-контроль в гонке не должен зависеть от сайта.
"""
from __future__ import annotations

import threading
import time

TICK_S = 2.0          # как часто смотрим на сессию
BIND_RETRY_S = 30.0   # повтор поиска этапа, пока он не найден
SYNC_EVERY_S = 5.0    # отправка решений


class LiveLink:
    def __init__(self, module) -> None:
        self.m = module
        self.sub = 0                    # для какой сессии ищем этап
        self.stage: dict | None = None
        self.candidates: list[dict] = []
        self.message = ""
        self.offline = False
        self.pending = 0                # решений ждёт отправки
        self._next_bind = 0.0
        self._next_sync = 0.0
        self._ids: dict = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- ресурсы ----------

    @property
    def _ctx(self):
        return self.m.ctx

    def _license(self):
        app = getattr(self._ctx, "app", None) if self._ctx else None
        return getattr(app, "license", None)

    def _bindings(self) -> dict:
        """Какой этап у какой сессии. Хранится в конфиге модуля: после
        перезапуска программы решения прошлой гонки всё равно найдут этап."""
        return self._ctx.config.setdefault("stage_bindings", {})

    # ---------- жизненный цикл ----------

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="rc-live")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def kick(self) -> None:
        """Отправить решения сейчас, не дожидаясь очереди."""
        self._next_sync = 0.0
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:                       # noqa: BLE001 — гонка важнее
                if self._ctx:
                    self._ctx.log.exception("лайв-судейство: сбой")
            self._wake.wait(TICK_S)
            self._wake.clear()

    def _tick(self) -> None:
        sim = self._ctx.sim
        snap = sim.snapshot()
        ids = sim.session_ids() if snap.connected and not getattr(sim, "demo", None) else {}
        sub = int(ids.get("subsession_id") or 0)
        now = time.monotonic()
        if sub and sub != self.sub:
            self.sub, self._ids = sub, ids
            self.stage = self._bindings().get(str(sub))
            self.candidates, self.message = [], ""
            self._next_bind = 0.0
            self._push()
        if sub and not self.stage and now >= self._next_bind:
            self._next_bind = now + BIND_RETRY_S
            self.bind()
        if now >= self._next_sync:
            self._next_sync = now + SYNC_EVERY_S
            self.sync()

    # ---------- этап ----------

    def bind(self, stage_plan_id: int = 0) -> dict:
        lic = self._license()
        if not lic or not self.sub:
            return {"ok": False, "error": "no_session",
                    "message": "В симе нет сессии с номером — привязывать нечего."}
        body = dict(self._ids, subsession_id=self.sub)
        if stage_plan_id:
            body["stage_plan_id"] = int(stage_plan_id)
        res = lic.api("/api/desktop/session/bind", body)
        self.offline = res.get("error") == "offline"
        if res.get("ok") and res.get("stage"):
            self.stage = res["stage"]
            self.candidates, self.message = [], ""
            self._bindings()[str(self.sub)] = self.stage
            self._ctx.save_config()
            self._ctx.log.info("лайв-судейство: сессия %s → этап %s (%s)", self.sub,
                               self.stage.get("id"), "авто" if res.get("auto") else "выбран")
            self.kick()
        elif res.get("ok"):
            self.candidates = res.get("candidates") or []
            self.message = res.get("message") or ""
        else:
            self.message = ("Нет связи с сайтом — решения сохраняются и уйдут позже."
                            if self.offline else res.get("message") or "Этап не определён.")
        self._push()
        return {"ok": bool(res.get("ok") and self.stage), **self.status(),
                **({"message": res.get("message")} if not res.get("ok") else {})}

    # ---------- решения ----------

    def sync(self) -> None:
        lic = self._license()
        st = self._ctx.storage
        rows = st.unsynced_decisions()
        marked = 0
        if not rows or not lic:
            self._set_pending(len(rows))
            return
        device = str(lic.state.get("device_id") or "")[:8]
        by_sub: dict[int, list] = {}
        for inc in rows:
            by_sub.setdefault(inc.subsession_id, []).append(inc)
        for sub, incs in by_sub.items():
            stage = self._bindings().get(str(sub))
            if not stage:
                continue                     # этап этой сессии ещё не определён
            refs = {f"{device}-{inc.id}": inc.id for inc in incs}
            res = lic.api("/api/desktop/decisions", {
                "subsession_id": str(sub), "stage_plan_id": stage["id"],
                "decisions": [self._payload(f"{device}-{inc.id}", inc) for inc in incs]})
            self.offline = res.get("error") == "offline"
            if res.get("ok"):
                ids = [refs[r] for r in res.get("saved") or [] if r in refs]
                st.mark_synced(ids)
                marked += len(ids)
            else:
                self._ctx.log.warning("лайв-судейство: решения не приняты: %s",
                                      res.get("error"))
        self._set_pending(len(st.unsynced_decisions()))
        if marked:
            # иначе лента перерисовывалась бы каждые 5 секунд без причины
            self.m._push_incidents()

    @staticmethod
    def _payload(ref: str, inc) -> dict:
        d = inc.decision_detail or {}
        return {
            "ref": ref, "outcome": inc.decision,
            "penalty": d.get("penalty"), "seconds": d.get("seconds"),
            "note": d.get("note") or "",
            # наказать могли соседа по инциденту, а не того, кто его получил
            "car_number": d.get("car") or inc.car_number,
            "driver_name": d.get("driver_name") or inc.driver_name,
            "customer_id": d.get("customer_id") or None,
            "involved": [c for c in ([inc.car_number] + list(inc.nearby or []))
                         if c != (d.get("car") or inc.car_number)],
            "kind": inc.kind, "inc_delta": inc.inc_delta,
            "session_num": inc.session_num, "session_time": inc.session_time,
            "lap": inc.lap, "decided_at": d.get("decided_at"),
        }

    def _set_pending(self, n: int) -> None:
        if n != self.pending:
            self.pending = n
            self._push()

    # ---------- состояние ----------

    def status(self) -> dict:
        lic = self._license()
        return {"subsession_id": self.sub, "stage": self.stage,
                "candidates": self.candidates, "message": self.message,
                "pending": self.pending, "offline": self.offline,
                "linked": bool(lic and lic.state.get("key"))}

    def _push(self) -> None:
        try:
            self._ctx.publish("rc_live", self.status())
        except Exception:                           # noqa: BLE001
            pass
