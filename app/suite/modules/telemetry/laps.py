"""Сводка круга и карта каналов — общие для живого потока и для `.ibt`.

Имена каналов зависят от машины: у `stockcars2 camaro2019` ход амортизатора
— `LFSHshockDefl`, у других — `LFshockDefl`. Поэтому модуль работает с
логическими именами (`shock_LF`), а карту «логическое -> имя в SDK» строит
по списку каналов, которые реально есть (`build_map`). Часть каналов
(высоты, сплиттер, горячие давления) iRacing отдаёт только в `.ibt` — в
живой сводке они `null`, а строка круга перечисляет их в `missing_live`.
"""
from __future__ import annotations

import math

CORNERS = ("LF", "RF", "LR", "RR")
RH_KEYS = ("LF", "RF", "LR", "RR", "CFSR")

# логический канал -> кандидаты имени в SDK, по порядку предпочтения
CANDIDATES: dict[str, list[str]] = {
    **{f"rh_{k}": [f"{k}rideHeight"] for k in RH_KEYS},
    **{f"shock_{c}": [f"{c}shockDefl", f"{c}SHshockDefl"] for c in CORNERS},
    **{f"shockvel_{c}": [f"{c}shockVel", f"{c}SHshockVel"] for c in CORNERS},
    **{f"pres_{c}": [f"{c}pressure"] for c in CORNERS},
    **{f"cold_{c}": [f"{c}coldPressure"] for c in CORNERS},
}
# группы для отметки «нет в live» в строке круга
GROUPS = {"ride_height": [f"rh_{k}" for k in RH_KEYS],
          "shock": [f"shock_{c}" for c in CORNERS],
          "pressure": [f"pres_{c}" for c in CORNERS]}

TIRE_T = {c: [f"{c}tempCL", f"{c}tempCM", f"{c}tempCR"] for c in CORNERS}
# температура поверхности по ходу езды: iRacing пишет её только в .ibt, в
# живом потоке каналов нет (CL/CM/CR выше обновляются лишь в боксе)
TIRE_S = {c: [f"{c}tempL", f"{c}tempM", f"{c}tempR"] for c in CORNERS}
G = 9.80665
TIRE_W = {c: [f"{c}wearL", f"{c}wearM", f"{c}wearR"] for c in CORNERS}

# каналы с постоянными именами
BASE = sorted({
    "SessionTime", "SessionTick", "SessionNum", "Lap", "LapCompleted", "LapDist", "LapDistPct",
    "LapCurrentLapTime", "LapLastLapTime",
    "Speed", "VelocityX", "VelocityY", "VelocityZ", "LatAccel", "LongAccel", "VertAccel",
    "YawRate", "Yaw", "Pitch", "Roll",
    "SteeringWheelAngle", "Throttle", "Brake", "Clutch", "Gear", "RPM",
    *[n for c in CORNERS for n in TIRE_T[c] + TIRE_W[c] + TIRE_S[c]],
    "FuelLevel", "PlayerTrackSurface", "OnPitRoad", "PlayerCarInPitStall", "SessionFlags",
    "PlayerCarMyIncidentCount", "IsOnTrack", "IsReplayPlaying",
    "IsDiskLoggingActive", "IsDiskLoggingEnabled",
    "TrackTempCrew", "AirTemp", "WindVel", "WindDir", "AirDensity",
})
# всё, что просим у отвода: постоянные + все кандидаты (отсутствующие придут None)
ALL_CHANNELS = sorted(set(BASE) | {n for c in CANDIDATES.values() for n in c})

TRK_OFF_TRACK = 0                                    # irsdk TrkLoc.off_track
YELLOW = 0x0008 | 0x0100 | 0x4000 | 0x8000           # yellow, yellow_waving, caution, caution_waving


def build_map(names) -> dict[str, str | None]:
    """Логический канал -> первое существующее имя (или None)."""
    have = set(names)
    return {k: next((n for n in cands if n in have), None) for k, cands in CANDIDATES.items()}


def missing_groups(m: dict) -> list[str]:
    return [g for g, keys in GROUPS.items() if all(m.get(k) is None for k in keys)]


def num(v):
    """Число из SDK или None; NaN/бесконечность — тоже None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def rnd(v, nd=4):
    return None if v is None else round(v, nd)


def tires(v: dict, m: dict) -> dict:
    out = {}
    for c in CORNERS:
        out[c] = {"temp": [rnd(num(v.get(n)), 2) for n in TIRE_T[c]],
                  "wear": [rnd(num(v.get(n)), 4) for n in TIRE_W[c]],
                  "pressure": rnd(num(v.get(m.get(f"pres_{c}") or "")), 2),
                  "cold_pressure": rnd(num(v.get(m.get(f"cold_{c}") or "")), 2)}
    return out


class Lap:
    """Сводка круга, копится по кадрам 60 Гц."""

    def __init__(self, v: dict, cfg: dict, m: dict):
        self.cfg, self.m = cfg, m
        self.rh_ch = {k: m.get(f"rh_{k}") for k in RH_KEYS}
        self.sh_ch = {c: m.get(f"shock_{c}") for c in CORNERS}
        self.t_start = num(v.get("SessionTime"))
        self.t_end = self.t_start
        self.fuel_start = num(v.get("FuelLevel"))
        self.fuel_end = self.fuel_start
        self.inc_start = v.get("PlayerCarMyIncidentCount")
        self.flags: set[str] = set()
        self.left_pits = not v.get("OnPitRoad")
        self.n = 0
        self.spd = [0.0, None, None]                 # сумма, min, max
        self.thr_full = self.brake_used = 0
        self.steer = {"corner": [0.0, 0], "straight": [0.0, 0]}
        self.lat = [0.0, 0, 0.0]                     # сумма |lat| в повороте, n, max |lat|
        self.vert_max = None
        self.rh = {k: {"c": [0.0, 0, None], "s": [0.0, 0]} for k in RH_KEYS}
        self.shock = {c: [None, None] for c in CORNERS}
        self.tires_first = None
        self.last = v
        # нагрузка на резину: перегрузки по осям и «работа» — ∫|a|·v dt
        self.t_prev = self.t_start
        self.g = {"lat_int": 0.0, "brake_int": 0.0, "brake_max": 0.0, "acc_max": 0.0,
                  "comb_max": 0.0, "work": 0.0, "dur": 0.0}
        self.surf = {c: [[0.0, 0, None] for _ in range(3)] for c in CORNERS}
        self.track_temp_start = num(v.get("TrackTempCrew"))

    def add(self, v: dict) -> None:
        self.n += 1
        self.last = v
        t = num(v.get("SessionTime"))
        if t is not None:
            self.t_end = t
        f = num(v.get("FuelLevel"))
        if f is not None:
            self.fuel_end = f
        if v.get("OnPitRoad"):
            self.flags.add("pit_in" if self.left_pits else "pit_out")
        else:
            self.left_pits = True
        if v.get("PlayerTrackSurface") == TRK_OFF_TRACK:
            self.flags.add("off_track")
        inc = v.get("PlayerCarMyIncidentCount")
        if inc is not None and self.inc_start is not None and inc > self.inc_start:
            self.flags.add("incident")
        if int(v.get("SessionFlags") or 0) & YELLOW:
            self.flags.add("yellow")

        s = num(v.get("Speed"))
        if s is not None:
            self.spd[0] += s
            self.spd[1] = s if self.spd[1] is None else min(self.spd[1], s)
            self.spd[2] = s if self.spd[2] is None else max(self.spd[2], s)
        if (num(v.get("Throttle")) or 0) > 0.98:
            self.thr_full += 1
        if (num(v.get("Brake")) or 0) > 0.02:
            self.brake_used += 1
        va = num(v.get("VertAccel"))
        if va is not None:
            self.vert_max = va if self.vert_max is None else max(self.vert_max, va)

        lat = abs(num(v.get("LatAccel")) or 0.0)
        steer = abs(num(v.get("SteeringWheelAngle")) or 0.0)
        corner = lat > self.cfg["corner_lat"]
        straight = lat < self.cfg["straight_lat"] and steer < self.cfg["straight_steer"]
        if lat > self.lat[2]:
            self.lat[2] = lat
        if corner:
            self.lat[0] += lat
            self.lat[1] += 1
            self.steer["corner"][0] += steer
            self.steer["corner"][1] += 1
        elif straight:
            self.steer["straight"][0] += steer
            self.steer["straight"][1] += 1
        for k, ch in self.rh_ch.items():
            if not ch:
                continue
            h = num(v.get(ch))
            if h is None:
                continue
            a = self.rh[k]
            if corner:
                a["c"][0] += h
                a["c"][1] += 1
                a["c"][2] = h if a["c"][2] is None else min(a["c"][2], h)
            elif straight:
                a["s"][0] += h
                a["s"][1] += 1
        for c, ch in self.sh_ch.items():
            if not ch:
                continue
            d = num(v.get(ch))
            if d is None:
                continue
            mm = self.shock[c]
            mm[0] = d if mm[0] is None else min(mm[0], d)
            mm[1] = d if mm[1] is None else max(mm[1], d)
        if self.tires_first is None:
            self.tires_first = tires(v, self.m)
        dt = (t - self.t_prev) if t is not None and self.t_prev is not None else 0.0
        if t is not None:
            self.t_prev = t
        if 0 < dt < 0.2:
            la = num(v.get("LatAccel")) or 0.0
            lo = num(v.get("LongAccel")) or 0.0
            cg = (la * la + lo * lo) ** 0.5
            g = self.g
            g["dur"] += dt
            g["lat_int"] += abs(la) * dt
            if lo < 0:
                g["brake_int"] += -lo * dt
                g["brake_max"] = max(g["brake_max"], -lo)
            else:
                g["acc_max"] = max(g["acc_max"], lo)
            g["comb_max"] = max(g["comb_max"], cg)
            g["work"] += cg * (s or 0.0) * dt
        for c in CORNERS:
            for i, n in enumerate(TIRE_S[c]):
                x = num(v.get(n))
                if x is None:
                    continue
                a = self.surf[c][i]
                a[0] += x
                a[1] += 1
                a[2] = x if a[2] is None else max(a[2], x)

    def summary(self, lap_no: int, lap_time, setup_id, session_num, missing=None) -> dict:
        v = self.last
        n = max(1, self.n)
        mean = lambda a: rnd(a[0] / a[1]) if a[1] else None          # noqa: E731
        tr = tires(v, self.m)
        stale = bool(self.tires_first) and tr == self.tires_first
        row = {
            "lap": lap_no, "setup_id": setup_id, "session_num": session_num,
            "t_start": rnd(self.t_start, 3), "t_end": rnd(self.t_end, 3),
            "lap_time": rnd(lap_time, 4),
            "valid": not self.flags, "flags": sorted(self.flags),
            "fuel_start": rnd(self.fuel_start, 3), "fuel_end": rnd(self.fuel_end, 3),
            "fuel_used": rnd(self.fuel_start - self.fuel_end, 3)
            if self.fuel_start is not None and self.fuel_end is not None else None,
            "track_temp": rnd(num(v.get("TrackTempCrew")), 2), "air_temp": rnd(num(v.get("AirTemp")), 2),
            "wind_vel": rnd(num(v.get("WindVel")), 2), "wind_dir": rnd(num(v.get("WindDir")), 3),
            "speed": {"max": rnd(self.spd[2], 3), "min": rnd(self.spd[1], 3),
                      "mean": rnd(self.spd[0] / n, 3) if self.n else None},
            "throttle_full_pct": round(100.0 * self.thr_full / n, 1),
            "brake_used_pct": round(100.0 * self.brake_used / n, 1),
            "steer": {"corner_mean_abs": mean(self.steer["corner"]),
                      "straight_mean_abs": mean(self.steer["straight"])},
            "lat_accel": {"corner_mean": mean(self.lat[:2]), "max": rnd(self.lat[2], 3)},
            "vert_accel_max": rnd(self.vert_max, 3),
            "ride_height": {k: {"corner_mean": mean(a["c"][:2]), "corner_min": rnd(a["c"][2]),
                                "straight_mean": mean(a["s"])} for k, a in self.rh.items()},
            "shock_defl": {c: {"min": rnd(mm[0]), "max": rnd(mm[1])} for c, mm in self.shock.items()},
            "tires": tr, "tires_stale": stale,
            "tire_wear_pct": {c: [None if w is None else round(w * 100, 1) for w in tr[c]["wear"]]
                              for c in CORNERS},
            # перегрузки в g; work — ∫|a|·v dt, кДж на кг массы: сколько работы
            # круг потребовал от резины (для модели износа)
            "g": {"lat_mean": rnd(self.g["lat_int"] / self.g["dur"] / G, 3) if self.g["dur"] else None,
                  "lat_int": rnd(self.g["lat_int"] / G, 2),
                  "brake_max": rnd(self.g["brake_max"] / G, 3), "brake_int": rnd(self.g["brake_int"] / G, 2),
                  "accel_max": rnd(self.g["acc_max"] / G, 3), "comb_max": rnd(self.g["comb_max"] / G, 3),
                  "work_kj_kg": rnd(self.g["work"] / 1000, 2)},
            "tire_surface": ({c: {"mean": [rnd(a[0] / a[1], 1) if a[1] else None for a in self.surf[c]],
                                  "max": [rnd(a[2], 1) for a in self.surf[c]]} for c in CORNERS}
                             if any(a[1] for c in CORNERS for a in self.surf[c]) else None),
            "track_temp_start": rnd(self.track_temp_start, 2),
            "samples": self.n,
            "draft": None,
        }
        if missing:
            row["missing_live"] = list(missing)
        return row


def _duration(lap: Lap):
    return (lap.t_end - lap.t_start) if lap.t_end is not None and lap.t_start is not None else None


class LapTracker:
    """Нарезка кругов по LapCompleted — одна для живого потока и для `.ibt`.

    Время круга сим пишет в LapLastLapTime не сразу после линии, а через
    1–3 с; до этого там время ПРЕДЫДУЩЕГО круга (проверено по .ibt). Поэтому
    круг ждёт, пока значение сменится (до WAIT_MAX с), и только тогда
    готов. Не сменилось — время считаем от линии до линии по SessionTime.
    """
    WAIT_MAX = 5.0

    def __init__(self, cfg: dict, m: dict):
        self.cfg, self.m = cfg, m
        self.lap: Lap | None = None
        self.lc = None
        self.pending: list[list] = []        # [круг, номер, время линии, LapLastLapTime на линии, сетап]

    def feed(self, v: dict, now: float, setup_id) -> None:
        """Кадр своей машины на трассе."""
        lc = v.get("LapCompleted")
        if self.lap is None:
            self.lap, self.lc = Lap(v, self.cfg, self.m), lc
        elif lc is not None and self.lc is not None and lc > self.lc:
            self.pending.append([self.lap, int(lc), now, num(v.get("LapLastLapTime")), setup_id])
            self.lap, self.lc = Lap(v, self.cfg, self.m), lc
        elif lc is not None and self.lc is not None and lc < self.lc:
            # сессию сбросили: счёт кругов пошёл заново — круг начинаем с нуля
            self.lap, self.lc = Lap(v, self.cfg, self.m), lc
        self.lap.add(v)

    def ready(self, last_lap_time, now: float, force: bool = False) -> list[tuple]:
        """Готовые круги: (сводка, номер, время круга, сетап)."""
        out = []
        llt = num(last_lap_time)
        while self.pending:
            lap, no, t0, llt0, sid = self.pending[0]
            dur = _duration(lap)
            changed = llt is not None and llt > 0 and llt != llt0
            if not (changed or force or now - t0 >= self.WAIT_MAX):
                break
            self.pending.pop(0)
            # новое значение появляется только после линии — это время этого круга
            # (сим считает его по-своему: у первого круга после выезда оно больше)
            t = llt if changed else dur
            out.append((lap, no, t, sid))
            llt = None                         # следующему кругу это значение не принадлежит
        return out

    def reset(self) -> None:
        self.lap, self.lc, self.pending = None, None, []
