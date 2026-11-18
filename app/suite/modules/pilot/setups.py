"""Сравнение сетапов: свои сектора и круги — отдельно по каждому сетапу.

Сетап узнаётся по содержимому раздела CarSetup из данных сессии (то, что
видно в гараже): открыли другой файл или поменяли хоть одну цифру — это
новый сетап, новый столбец. Вернулись к прежнему содержимому — круги идут
в его старый столбец. Подпись — имя файла (`DriverSetupName`) и «изм. N»,
если сетап правили после загрузки.

Свои сектора считаем по `LapDistPct` на 60 Гц: момент пересечения границы
интерполируется между соседними замерами (см. broadcast/sectors.py).
Сектор «грязный», если в нём были пит-лейн, инцидент или смена сетапа:
такие в лучшее и среднее не идут. Круг — сумма трёх секторов одного
прохода, если все они чистые.

Сбрасывается при смене трассы или машины.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time


# Что в CarSetup не настройка, а замер или следствие топлива: iRacing переписывает это
# после каждого пит-стопа (горячее давление, температуры и остаток протектора с
# последней остановки) и при смене топлива (вес на колёсах, клиренс). По ним тот же
# файл сетапа после заезда в бокс выглядел новым — столбцы-двойники.
NOT_SETUP = ("updatecount", "lasthotpressure", "lasttemps", "treadremaining",
             "cornerweight", "rideheight", "fuellevel", "defl", "atrest")


def _settings(d):
    if isinstance(d, dict):
        return {k: _settings(v) for k, v in d.items()
                if not any(x in str(k).lower() for x in NOT_SETUP)}
    return d


def setup_hash(car_setup: dict | None) -> str:
    """Отпечаток настроек сетапа: одинаковые настройки — один сетап, что бы ни
    намерил сим на пит-стопе."""
    if not car_setup:
        return ""
    d = _settings(car_setup)
    return hashlib.sha1(json.dumps(d, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:12]


def _valid_name(name: str) -> bool:
    """Имя файла сетапа есть: пока сим его не знает, он отдаёт «INVALID_LOADTYPE» или пусто."""
    n = str(name or "").strip()
    return bool(n) and "invalid" not in n.lower()


def _file_name(name: str) -> str:
    name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return name[:-4] if name.lower().endswith(".sto") else (name or "сетап")


class SetupLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    # ---------- на диск: перезапуск программы посреди сессии не стирает сетапы ----------

    KEEP_S = 4 * 3600                         # сохранённое старше — уже другая сессия

    def dump(self) -> dict:
        with self._lock:
            return {"key": list(self.key) if self.key else None, "saved": time.time(),
                    "setups": [{k: v for k, v in e.items()} for e in self.setups]}

    def load(self, d: dict) -> bool:
        """Поднять сохранённое; ключ (трасса, машина) сверит первая check()."""
        try:
            if not d or time.time() - float(d.get("saved") or 0) > self.KEEP_S or not d.get("key"):
                return False
            with self._lock:
                self.key = tuple(d["key"])
                self.setups = [dict(e) for e in d.get("setups") or []]
                self.version += 1
            return True
        except (TypeError, ValueError):
            return False

    def reset(self, key=None) -> None:
        self.key = key
        self.setups: list[dict] = []
        self.version = getattr(self, "version", 0) + 1    # растёт на каждое изменение — когда сохранять
        self.cur: str = ""
        self._run = None                       # проход по кругу: где машина, сектора
        self._starts: list[float] = []

    # ---------- какой сетап (поток модуля, 4 раза в секунду) ----------

    def check(self, key, sid: str, name: str, modified: bool) -> None:
        with self._lock:
            if key != self.key:
                self.reset(key)
            if not sid or not _valid_name(name):
                return                       # файл ещё не загружен — столбец не заводим
            e = next((x for x in self.setups if x["id"] == sid), None)
            if e is None:
                fn = _file_name(name)
                edit = 0
                if modified:
                    edit = 1 + sum(1 for x in self.setups if x["file"] == fn and x["edit"])
                e = {"id": sid, "file": fn, "edit": edit, "sec": [], "laps": [], "first": time.time()}
                self.setups.append(e)
                self.version += 1
            e["used"] = time.time()
            self.cur = sid

    def set_starts(self, starts: list[float]) -> None:
        with self._lock:
            if starts != self._starts:
                self._starts = list(starts)
                for e in self.setups:          # другая разметка — сектора несравнимы
                    e["sec"], e["laps"] = [], []
                self._run = None

    # ---------- свои сектора (отвод 60 Гц) ----------

    def tick(self, p, t, on_track: bool, pit: bool, inc: int) -> None:
        with self._lock:
            n = len(self._starts)
            if n < 2 or p is None or t is None or not on_track or p < 0 or not self.cur:
                self._run = None
                return
            p, t = float(p), float(t)
            s = self._sector_of(p)
            r = self._run
            if r is None or t <= r["t"] - 1.0:
                self._run = {"p": p, "t": t, "sec": s, "enter": None, "inc": inc, "setup": self.cur,
                             "vals": [None] * n, "dirty": [False] * n}
                return
            if t <= r["t"]:
                return
            if pit or inc > r["inc"] or self.cur != r["setup"]:
                r["dirty"][r["sec"]] = True
            r["inc"] = max(r["inc"], inc)
            if s != r["sec"]:
                if s == (r["sec"] + 1) % n:
                    tc = _cross_time(r["p"], r["t"], p, t, self._starts[s])
                    done = r["sec"]
                    if r["enter"] is not None:
                        dur = tc - r["enter"]
                        if 1.0 < dur < 600.0:
                            r["vals"][done] = round(dur, 3)
                            self._put_sector(r["setup"], done, r["vals"][done], r["dirty"][done])
                    r["enter"] = tc
                    if s == 0:
                        if all(v is not None for v in r["vals"]) and not any(r["dirty"]):
                            self._put_lap(r["setup"], list(r["vals"]))
                        r["vals"], r["dirty"] = [None] * n, [False] * n
                    r["dirty"][s] = False
                    r["setup"] = self.cur
                else:
                    r["enter"] = None          # перескок сектора: телепорт, сброс в боксы
                    r["vals"], r["dirty"] = [None] * n, [True] * n
                r["sec"] = s
            r["p"], r["t"] = p, t

    def _sector_of(self, p: float) -> int:
        s = 0
        for i, b in enumerate(self._starts):
            if p >= b:
                s = i
        return s

    def _entry(self, sid):
        return next((x for x in self.setups if x["id"] == sid), None)

    def _put_sector(self, sid, k, v, dirty):
        e = self._entry(sid)
        if e is None or dirty:
            return
        while len(e["sec"]) <= k:
            e["sec"].append([])
        e["sec"][k].append(v)
        self.version += 1

    def _put_lap(self, sid, vals):
        e = self._entry(sid)
        if e is not None:
            e["laps"].append(round(sum(vals), 3))
            self.version += 1

    # ---------- выдача ----------

    def summary(self, mode: str) -> tuple[list[dict], int]:
        with self._lock:
            n = len(self._starts) or 3
            out = []
            for e in self.setups:
                secs = [(e["sec"][k] if k < len(e["sec"]) else []) for k in range(n)]
                best = [min(x) if x else None for x in secs]
                avg = [round(sum(x) / len(x), 3) if x else None for x in secs]
                laps = e["laps"]
                lap = (min(laps) if mode != "avg" else round(sum(laps) / len(laps), 3)) if laps else None
                theo = round(sum(best), 3) if all(v is not None for v in best) else None
                out.append({"id": e["id"], "name": e["file"], "edit": e["edit"], "cur": e["id"] == self.cur,
                            "used": e.get("used", 0), "s": avg if mode == "avg" else best,
                            "lap": lap, "theo": theo, "laps": len(laps),
                            "avg": round(sum(laps) / len(laps), 3) if laps else None})
            return out, n


def _cross_time(p0, t0, p1, t1, b):
    if p1 < p0:                                # проехали линию старта
        total = (1.0 - p0) + p1
        part = (1.0 - p0) + (b if b > 0 else 0.0)
    else:
        total = p1 - p0
        part = b - p0
    frac = part / total if total > 0 else 1.0
    return t0 + min(1.0, max(0.0, frac)) * (t1 - t0)


# демо: пять сетапов сессии, текущий — четвёртый (как в мокапе)
DEMO = [("lvms_race_v3", 0, 12, [10.212, 10.604, 9.731], [10.268, 10.661, 9.790]),
        ("lvms_race_v3", 1, 6, [10.198, 10.631, 9.712], [10.251, 10.702, 9.771]),
        ("baseline", 0, 4, [10.255, 10.581, 9.760], [10.320, 10.640, 9.812]),
        ("lvms_race_v3", 2, 3, [10.181, 10.610, 9.705], [10.236, 10.668, 9.760]),
        ("drafting_v4", 0, 8, [10.230, 10.592, 9.744], [10.290, 10.650, 9.801])]


def _demo_tyres(i: int) -> dict:
    k = 1 + i * .06
    base = {"LF": ([.18, .16, .14], [71, 76, 79], 179), "RF": ([.37, .32, .27], [97, 91, 84], 311),
            "LR": ([.14, .12, .10], [64, 66, 67], 179), "RR": ([.26, .23, .20], [83, 80, 77], 310)}
    return {c: {"wear": [round(w * k, 3) for w in ws], "temp": [round(t + i) for t in ts], "pres": p + i}
            for c, (ws, ts, p) in base.items()}


def demo_summary(mode: str) -> tuple[list[dict], int]:
    out = []
    for i, (name, edit, laps, best, avg) in enumerate(DEMO):
        s = avg if mode == "avg" else best
        lap = round(sum(s) + (0.0 if mode == "avg" else 0.021), 3)
        out.append({"id": f"demo{i}", "name": name, "edit": edit, "cur": i == 3, "used": i + (10 if i == 3 else 0),
                    "s": s, "lap": lap, "theo": round(sum(best), 3), "laps": laps,
                    "avg": round(sum(avg) + .05, 3), "trk": 33 + i, "fuel": round(1.36 + i * .01, 3),
                    "tyres": _demo_tyres(i) if i != 2 else None, "sessions": 1 + i % 3})
    return out, 3
