"""Режиссёр трансляции: повтор инцидента, камера на инцидент, автосмена камер.

Инциденты — свои, по счётчикам инков из данных сессии (CurDriverIncidentCount):
трансляции не нужен включённый рейс-контроль. Счётчик приходит с задержкой
1–2 с, поэтому повтор начинается с запасом (pre_roll).

Повтор к инциденту:
  1. шторка закрывает экран (fx: in → cover), чтобы зритель не видел перемотку;
  2. перемотка к времени сессии − pre_roll (команда дважды: первая из live только
     переводит сим в повтор, см. core/commands.py), камера на машину с инцидентом,
     воспроизведение с обычной скоростью;
  3. сим ищет кадр; шторка открывается (out). В LIVE — тем же путём.

Автосмена камер: раз в N секунд следующая группа из выбранных, машина та же —
пилотов режиссёр переключает сам. Камера на инцидент: новый инцидент — машина
в кадр, автосмена HOLD_S секунд не уводит с неё вид.

Всё работает и в повторе: broadcast-команды сим принимает в любом режиме.
"""
from __future__ import annotations

import threading
import time
from collections import deque

try:
    import irsdk
except ImportError:                      # демо и тесты без pyirsdk
    irsdk = None

DEFAULTS = {
    "inc_cam": False,        # камера на машину с новым инцидентом
    "inc_min": 2,            # от скольких x: 1 — и вылеты, 2 — потеря контроля, 4 — контакты
    "auto_cam": False,       # автосмена камер
    "auto_groups": [],       # имена групп по кругу
    "auto_every": 12,        # секунд на камеру
    "pre_roll": 6,           # секунд до инцидента в повторе
}
HOLD_S = 8.0                 # после камеры на инцидент автосмена ждёт
FX_SEEK = 1.4                # без сима (демо): столько держим шторку
# с симом шторка открывается, когда повтор реально пошёл (см. _settle), но не раньше
# COVER_MIN — логотип успевает проявиться — и не позже COVER_MAX
COVER_MIN, COVER_MAX = 0.6, 4.0
# закрытие и открытие шторки по вариантам, с — как в core/ui/fx_paint.py и web/ov_replay.html
FX_TIMES = {"A": (0.5, 0.58), "B": (0.66, 1.0), "C": (0.55, 0.6), "D": (0.5, 0.85)}
LIVE_TAG_S = 3.0             # метка «● LIVE» после возврата
KEEP = 30                    # инцидентов в журнале пульта


class Director:
    def __init__(self, bc) -> None:
        self.bc = bc
        self.incidents: deque = deque(maxlen=KEEP)
        self._id = 0
        self._next_auto = 0.0
        self._auto_i = -1
        self._hold_until = 0.0
        self.fx = {"phase": "", "at": 0.0, "n": 0, "kind": ""}   # шторка повтора
        self.replay = None                   # инцидент, который сейчас в повторе
        self._live_at = 0.0                  # когда вернулись в LIVE (метка «● LIVE»)
        self._busy = threading.Lock()
        self._stop = threading.Event()

    # ---------- настройки ----------

    @property
    def cfg(self) -> dict:
        c = self.bc.ctx.config.setdefault("director", {})
        for k, v in DEFAULTS.items():
            c.setdefault(k, list(v) if isinstance(v, list) else v)
        return c

    def set(self, data: dict) -> dict:
        c = self.cfg
        for k, d in DEFAULTS.items():
            if k not in data:
                continue
            v = data[k]
            try:
                c[k] = ([str(x) for x in v][:6] if isinstance(d, list) else bool(v) if isinstance(d, bool)
                        else max(1, min(120, int(v))))
            except (TypeError, ValueError):
                pass
        if "auto_cam" in data:
            self._next_auto = 0.0
        self.bc.ctx.save_config()
        return {"ok": True, **self.state()}

    def state(self) -> dict:
        now = time.monotonic()
        snap = self.bc.sim.snapshot()
        return {"cfg": dict(self.cfg), "live": snap.mode == "LIVE",
                "replay": dict(self.replay) if self.replay else None,
                "fx": self.fx_state(), "next_cam_in": round(max(0.0, self._next_auto - now), 1)
                if self.cfg.get("auto_cam") else None,
                "incidents": list(reversed(self.incidents))}

    # ---------- инциденты (поток модуля, только в LIVE) ----------

    def on_incident(self, idx: int, delta: int, total: int) -> None:
        bc = self.bc
        snap = bc.sim.snapshot()
        row = next((r for r in bc._rows if r["car_idx"] == idx), None)
        if row is None:
            return
        self._id += 1
        inc = {"id": self._id, "car_idx": idx, "car_number": row["car_number"], "name": row["name"],
               "delta": delta, "total": total, "lap": int(row.get("lap") or 0),
               "session_num": snap.session_num, "session_time": round(float(snap.session_time), 1),
               "wall": time.time()}
        self.incidents.append(inc)
        c = self.cfg
        if c.get("inc_cam") and delta >= int(c.get("inc_min", 2)) and not self.replay:
            if bc.sim.switch_camera(row["car_number"], None):
                self._hold_until = time.monotonic() + HOLD_S

    # ---------- автосмена камер ----------

    def tick(self) -> None:
        c = self.cfg
        now = time.monotonic()
        # вернулись в LIVE сами (в симе или с клавиатуры) — повтор закончен
        if self.replay and not self.fx["phase"] and self.bc.sim.snapshot().mode == "LIVE":
            self.replay = None
            self._live_at = now
        if not c.get("auto_cam") or self.fx["phase"] in ("in", "cover"):
            return
        names = [n for n in (c.get("auto_groups") or []) if n]
        groups = self.bc.sim.camera_groups() or {}
        nums = [groups[n] for n in names if n in groups]
        if len(nums) < 2:
            return
        if now < self._next_auto or now < self._hold_until:
            return
        cur = (self.bc.sim.camera_state() or {}).get("group")
        i = nums.index(cur) if cur in nums else self._auto_i
        self._auto_i = (i + 1) % len(nums)
        self.bc.sim.switch_camera(None, nums[self._auto_i])
        self._next_auto = now + max(3, int(c.get("auto_every", 12)))

    # ---------- шторка и повтор ----------

    def fx_state(self) -> dict:
        """Фаза шторки для оверлея: in — закрывается, cover — закрыта, out — открывается."""
        f = dict(self.fx)
        at = f.pop("at", 0.0)
        v = self.variant()
        t_in, t_out = FX_TIMES[v]
        f["t"] = round(time.monotonic() - at, 3) if f["phase"] else 0.0
        f["dur"] = {"in": t_in, "cover": COVER_MAX, "out": t_out}.get(f["phase"], 0.0)   # cover — не дольше
        f["variant"] = v
        f["word"] = "LIVE" if f.get("kind") == "live" else "ПОВТОР"
        f["bug"] = self.bug()
        return f

    def variant(self) -> str:
        v = str(self.bc.ctx.config.get("replay_fx") or "D").upper()
        return v if v in FX_TIMES else "D"

    def bug(self) -> dict | None:
        """Метка в углу: «◀◀ ПОВТОР · номер · фамилия · круг», после возврата — «● LIVE»."""
        if not self.bc.ctx.config.get("replay_bug", True):
            return None
        ph = self.fx["phase"]
        if self.replay and ph not in ("in", "cover"):
            r = self.replay
            colors = []
            try:
                app = self.bc.ctx.app
                colors = (app.plates() if app is not None else {}).get(str(r["car_number"])) or []
            except Exception:                    # noqa: BLE001
                colors = []
            return {"mode": "replay", "num": r["car_number"], "name": r["name"], "lap": r.get("lap") or 0,
                    "colors": colors}
        if not self.replay and ph not in ("in", "cover") and time.monotonic() - self._live_at < LIVE_TAG_S:
            return {"mode": "live"}
        return None

    def _fx(self, phase: str, kind: str = "") -> None:
        self.fx = {"phase": phase, "at": time.monotonic(), "n": self.fx["n"] + (phase == "in"),
                   "kind": kind or self.fx.get("kind", "")}

    def _ir(self):
        sim = self.bc.sim
        if getattr(sim, "demo", False):
            return None
        ir = getattr(sim, "ir", None)
        return ir if ir is not None and getattr(ir, "is_connected", False) else None

    def show(self, inc_id: int) -> dict:
        inc = next((x for x in self.incidents if x["id"] == int(inc_id)), None)
        if inc is None:
            return {"ok": False, "error": "unknown_incident"}
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "busy"}
        threading.Thread(target=self._run, args=("replay", inc), daemon=True, name="director-replay").start()
        return {"ok": True}

    def live(self) -> dict:
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "busy"}
        threading.Thread(target=self._run, args=("live", None), daemon=True, name="director-live").start()
        return {"ok": True}

    @staticmethod
    def _frame(ir):
        try:
            return int(ir["ReplayFrameNum"]) if ir is not None else None
        except Exception:                    # noqa: BLE001
            return None

    def _settle(self, ir, kind: str, before, covered: float) -> str:
        """Ждать под шторкой, пока сим не встанет на место.

        В повтор: номер кадра ленты отскочил от того, где был до перемотки, и
        дальше дважды подряд растёт ровно, как при обычной скорости (около 60
        кадров в секунду) — повтор идёт. В LIVE: до конца ленты почти ничего
        (ReplayFrameNumEnd). Значения сим обновляет с частотой опроса, поэтому
        смотрим только на новые замеры.
        """
        t_end = covered + COVER_MAX
        if ir is None:                       # демо: сима нет — фиксированное время
            time.sleep(max(0.0, max(COVER_MIN, FX_SEEK) - (time.monotonic() - covered)))
            return "без сима"
        last = None                          # (кадр, время замера)
        steady = 0
        jumped = False
        why = "предел"
        while time.monotonic() < t_end:
            now = time.monotonic()
            if kind == "live":
                try:
                    left = int(ir["ReplayFrameNumEnd"] or 0)
                except Exception:            # noqa: BLE001
                    left = 0
                if left <= 10:
                    why = "лента в конце"
                    break
            else:
                f = self._frame(ir)
                if f is not None and (last is None or f != last[0]):
                    if before is not None and abs(f - before) > 90:
                        jumped = True
                    if last is not None and jumped:
                        rate = (f - last[0]) / max(1e-3, now - last[1])
                        steady = steady + 1 if 30 <= rate <= 100 else 0
                        if steady >= 2:
                            why = "повтор идёт"
                            break
                    last = (f, now)
            time.sleep(0.03)
        rest = COVER_MIN - (time.monotonic() - covered)
        if rest > 0:
            time.sleep(rest)
        return why

    def _run(self, kind: str, inc: dict | None) -> None:
        t_in, t_out = FX_TIMES[self.variant()]
        try:
            self._fx("in", kind)
            time.sleep(t_in)
            self._fx("cover")
            covered = time.monotonic()
            ir = self._ir()
            before = self._frame(ir)
            if kind == "replay":
                pre = float(self.cfg.get("pre_roll", 6))
                ms = int(max(0.0, inc["session_time"] - pre) * 1000)
                if ir is not None:
                    ir.replay_search_session_time(inc["session_num"], ms)
                    time.sleep(0.25)
                    ir.replay_search_session_time(inc["session_num"], ms)
                self.bc.sim.switch_camera(inc["car_number"], None)
                if ir is not None:
                    ir.replay_set_play_speed(1, False)
                self.replay = dict(inc)
            else:
                if ir is not None and irsdk is not None:
                    ir.replay_search(irsdk.RpySrchMode.to_end)
                    ir.replay_set_play_speed(1, False)
                self.replay = None
            how = self._settle(ir, kind, before, covered)
            self.bc.ctx.log.info("режиссёр: %s — шторка %.2f с (%s)", "повтор" if kind == "replay" else "в LIVE",
                                 time.monotonic() - covered, how)
            if kind == "live":
                self._live_at = time.monotonic()
            self._fx("out")
            time.sleep(t_out)
            self._fx("")
        except Exception:                    # noqa: BLE001
            self.bc.ctx.log.exception("режиссёр: повтор")
            self._fx("")
        finally:
            self._busy.release()
