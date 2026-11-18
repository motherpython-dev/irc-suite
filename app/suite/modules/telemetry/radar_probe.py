"""Сбор данных для радара: когда iRacing говорит «машина рядом».

iRacing не публикует, на каком расстоянии его споттер считает машину
соседней, а поперечного расстояния вовсе не отдаёт. Продольный порог можно
измерить: пока пилот едет в трафике, записываем каждое переключение
`CarLeftRight` вместе с расстоянием по трассе до ближайших машин, а пока
кто-то ближе 15 м — ещё и 10 замеров в секунду.

В `.ibt` этих данных нет (там только своя машина) — только вживую.

Файл: <папка данных>/telemetry/radar-probe/<дата-время>.jsonl, строки:
  {"t", "ev": "flip", "from", "to", "near": [[номер машины, м], ...]}
  {"t", "ev": "s", "clr", "near": [[номер, м], ...]}
м — расстояние по трассе от центра своей машины до центра чужой: «+» —
впереди, «−» — сзади. Сводка порогов — в summary().
"""
from __future__ import annotations

import json
import statistics
import threading
import time
from datetime import datetime
from pathlib import Path

CHANNELS = ["CarLeftRight", "CarIdxLapDistPct", "CarIdxTrackSurface", "PlayerCarIdx",
            "SessionTick", "IsOnTrack", "IsReplayPlaying"]
ON_TRACK = 3                     # irsdk_TrkLoc.on_track
CLEAR = (0, 1)                   # off, clear — рядом никого
LABEL = {0: "выкл", 1: "чисто", 2: "слева", 3: "справа", 4: "с двух сторон",
         5: "двое слева", 6: "двое справа"}
SAMPLE_NEAR_M = 15.0
SAMPLE_EVERY_S = 0.1


class RadarProbe:
    def __init__(self, sim, root: Path, log) -> None:
        self.sim, self.root, self.log = sim, root, log
        self._tap = None
        self._lock = threading.Lock()
        self._file = None
        self.path: Path | None = None
        self._tick = None
        self._prev = None
        self._next_sample = 0.0
        self._len_m = None
        self._len_at = 0.0
        self.flips: list[dict] = []      # переключения за этот сбор
        self.samples = 0
        self.frames = 0

    @property
    def on(self) -> bool:
        return self._tap is not None

    # ---------- вкл / выкл ----------

    def start(self) -> None:
        if self._tap is not None:
            return
        d = self.root / "radar-probe"
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / (datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".jsonl")
        self._file = self.path.open("a", encoding="utf-8")
        self.flips, self.samples, self.frames = [], 0, 0
        self._tick = self._prev = None
        self._tap = self.sim.add_tap(CHANNELS, 60.0, self._frame)
        self.log.info("радар: сбор данных включён → %s", self.path)

    def stop(self) -> None:
        if self._tap is None:
            return
        self.sim.remove_tap(self._tap)
        self._tap = None
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None
        self.log.info("радар: сбор данных выключен, переключений %d", len(self.flips))

    # ---------- кадр 60 Гц (поток опроса сима) ----------

    def _track_m(self) -> float | None:
        now = time.monotonic()
        if self._len_m is None or now - self._len_at > 10:
            self._len_at = now
            try:
                km = self.sim.weather().get("length_km")
                self._len_m = km * 1000 if km else None
            except Exception:                    # noqa: BLE001
                self._len_m = None
        return self._len_m

    def _frame(self, v: dict) -> None:
        if not v.get("IsOnTrack") or v.get("IsReplayPlaying"):
            return
        tick = v.get("SessionTick")
        if tick is not None and tick == self._tick:
            return
        self._tick = tick
        self.frames += 1
        clr = v.get("CarLeftRight")
        pct, surf, me = v.get("CarIdxLapDistPct"), v.get("CarIdxTrackSurface"), v.get("PlayerCarIdx")
        L = self._track_m()
        if clr is None or not pct or me is None or not L or me >= len(pct):
            return
        clr, me = int(clr), int(me)
        near = []
        for i, p in enumerate(pct):
            if i == me or p is None or p < 0 or (surf and i < len(surf) and surf[i] != ON_TRACK):
                continue
            d = ((p - pct[me]) % 1.0 + 1.5) % 1.0 - 0.5          # доля круга в [-0.5, 0.5)
            near.append((i, round(d * L, 2)))
        near.sort(key=lambda x: abs(x[1]))
        near = near[:4]
        now = time.monotonic()
        rec = None
        if self._prev is not None and clr != self._prev:
            rec = {"t": round(now, 3), "ev": "flip", "from": self._prev, "to": clr,
                   "near": [[self._num(i), m] for i, m in near]}
            self.flips.append(rec)
        elif near and abs(near[0][1]) < SAMPLE_NEAR_M and now >= self._next_sample:
            self._next_sample = now + SAMPLE_EVERY_S
            rec = {"t": round(now, 3), "ev": "s", "clr": clr, "near": [[self._num(i), m] for i, m in near]}
            self.samples += 1
        self._prev = clr
        if rec:
            with self._lock:
                if self._file:
                    self._file.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    self._file.flush()

    def _num(self, idx: int) -> str:
        d = getattr(self.sim, "_drivers", {}).get(idx)
        return d.car_number if d else str(idx)

    # ---------- сводка ----------

    def summary(self) -> dict:
        """Продольное расстояние до ближайшей машины в момент, когда признак
        «рядом» включился и когда выключился: мин / медиана / макс, м."""
        def stats(xs):
            if not xs:
                return None
            xs = sorted(xs)
            return {"n": len(xs), "min": xs[0], "median": round(statistics.median(xs), 2), "max": xs[-1]}

        on = [abs(f["near"][0][1]) for f in self.flips
              if f["near"] and f["from"] in CLEAR and f["to"] not in CLEAR]
        off = [abs(f["near"][0][1]) for f in self.flips
               if f["near"] and f["from"] not in CLEAR and f["to"] in CLEAR]
        return {"on": self.on, "file": str(self.path) if self.path else "",
                "frames": self.frames, "samples": self.samples, "flips": len(self.flips),
                "turn_on_m": stats(on), "turn_off_m": stats(off),
                "last": [{"from": LABEL.get(f["from"], f["from"]), "to": LABEL.get(f["to"], f["to"]),
                          "near": f["near"][:2]} for f in self.flips[-5:]]}
