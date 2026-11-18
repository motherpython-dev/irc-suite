"""История сетапов из записанной телеметрии: по трассе и машине, по сетапам.

Окно «Сетапы» показывает не только текущую сессию, но и прошлые заезды. Всё
нужное уже лежит в папках сессий телеметрии (только свои заезды — запись идёт
на компьютере пилота):

  session.json        машина (CarPath) и трасса (TrackName);
  setups/NNN.yaml     копия раздела CarSetup, NNN.meta.json — имя файла сетапа;
  laps.jsonl          сводки кругов: номер сетапа, время, чистый ли, топливо,
                      температура трассы, износ (обновляется только в боксе),
                      круги на комплекте; строки-события (пит-стоп) пропускаем;
  laps_ibt.jsonl      те же круги из .ibt: износ, температуры и горячее давление
                      колёс на КАЖДОМ круге — точнее; есть, когда .ibt разобран.

Сетап узнаётся по настройкам (setup_hash — тот же, что у живого окна): один и
тот же сетап из разных дней сводится в один столбец.

Резина по сетапу — по трём частям колеса, как отдаёт iRacing (L, M, R):
  износ за круг, % — по .ibt: средняя убыль за круг на одном комплекте; без
  .ibt — по замерам в боксе: (100 − остаток) / круги на комплекте;
  температура, °C — по .ibt: средняя за чистые круги; без .ibt — последний замер в боксе;
  горячее давление, kPa — только из .ibt (в живом потоке его нет).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

try:
    import yaml
except ImportError:                          # без PyYAML истории нет — окно покажет только сессию
    yaml = None

from ..pilot.setups import setup_hash, _file_name

CORNERS = ("LF", "RF", "LR", "RR")
CACHE_S = 10.0


def _read_jsonl(p: Path) -> list[dict]:
    out = []
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _mean(a):
    a = [x for x in a if x is not None]
    return sum(a) / len(a) if a else None


class SetupHistory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict = {}               # (машина, трасса, папка) -> (когда, ответ)
        self._hash: dict = {}                # путь yaml -> (mtime, отпечаток)

    def _setup_ids(self, folder: Path) -> dict[int, tuple[str, str, bool]]:
        """Номер сетапа в сессии -> (отпечаток настроек, имя файла, правленый ли)."""
        out = {}
        for meta in sorted((folder / "setups").glob("*.meta.json")):
            try:
                m = json.loads(meta.read_text("utf-8"))
                y = meta.with_name(meta.name.replace(".meta.json", ".yaml"))
                mt = y.stat().st_mtime
                hit = self._hash.get(str(y))
                if hit and hit[0] == mt:
                    h = hit[1]
                else:
                    h = setup_hash(yaml.safe_load(y.read_text("utf-8")) or {})
                    self._hash[str(y)] = (mt, h)
                out[int(m.get("setup_id") or 0)] = (h, _file_name(m.get("setup_name") or ""), bool(m.get("is_modified")))
            except Exception:                # noqa: BLE001 — битый файл сетапа пропускаем
                continue
        return out

    def get(self, car_path: str, track: str, only_dir: str | None = None) -> dict:
        """{отпечаток сетапа: сводка}. only_dir — только эта папка сессии (текущая)."""
        if yaml is None or not self.root or not car_path or not track:
            return {}
        key = (car_path.lower(), track.lower(), only_dir)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        agg: dict = {}
        dirs = [self.root / only_dir] if only_dir else sorted(p for p in self.root.iterdir() if p.is_dir())
        for folder in dirs:
            try:
                s = json.loads((folder / "session.json").read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if str((s.get("car") or {}).get("CarPath") or "").lower() != key[0]:
                continue
            if str((s.get("track") or {}).get("TrackName") or "").lower() != key[1]:
                continue
            ids = self._setup_ids(folder)
            laps = [r for r in _read_jsonl(folder / "laps.jsonl") if "lap_time" in r and not r.get("event")]
            ibt = [r for r in _read_jsonl(folder / "laps_ibt.jsonl") if "lap_time" in r]
            for r in laps:
                sid = ids.get(int(r.get("setup_id") or 0))
                if not sid:
                    continue
                a = agg.setdefault(sid[0], {"name": sid[1], "edit": sid[2], "dirs": set(), "times": [], "fuel": [],
                                            "best_trk": None, "best": None, "rate": None, "temp": None, "pres": None,
                                            "_pit": None, "_ibt": [], "used": 0.0})
                a["dirs"].add(folder.name)
                a["used"] = max(a["used"], folder.stat().st_mtime)
                if r.get("valid") and r.get("lap_time"):
                    t = float(r["lap_time"])
                    a["times"].append(t)
                    if r.get("fuel_used") is not None and r["fuel_used"] > 0:
                        a["fuel"].append(float(r["fuel_used"]))
                    if a["best"] is None or t < a["best"]:
                        a["best"], a["best_trk"] = t, r.get("track_temp")
                # замер в боксе: остаток протектора сменился — запоминаем последний
                wp = r.get("tire_wear_pct") or {}
                if wp and r.get("tyre_laps") and any(w is not None and w < 99.95 for c in wp.values() for w in c):
                    a["_pit"] = (wp, (r.get("tires") or {}), int(r.get("tyre_laps") or 1))
            for r in ibt:
                sid = ids.get(int(r.get("setup_id") or 0))
                if sid and sid[0] in agg:
                    agg[sid[0]]["_ibt"].append(r)
        out = {}
        for h, a in agg.items():
            ty = self._tyres(a)
            times = a["times"]
            out[h] = {"name": a["name"], "edit": a["edit"], "sessions": len(a["dirs"]), "laps": len(times),
                      "best": round(min(times), 3) if times else None,
                      "avg": round(sum(times) / len(times), 3) if times else None,
                      "trk": round(a["best_trk"], 1) if a["best_trk"] is not None else None,
                      "fuel": round(_mean(a["fuel"]), 3) if a["fuel"] else None,
                      "tyres": ty, "used": a["used"]}
        self._cache[key] = (time.monotonic(), out)
        return out

    @staticmethod
    def _tyres(a: dict) -> dict | None:
        """{колесо: {"wear": [L, M, R] % за круг, "temp": [L, M, R] °C, "pres": kPa}}."""
        rows = [r for r in a["_ibt"] if r.get("tires")]
        if len(rows) >= 2:
            out = {}
            for c in CORNERS:
                drops = [[], [], []]
                temps = [[], [], []]
                pres = []
                prev = None
                for r in rows:
                    t = (r.get("tires") or {}).get(c) or {}
                    w = t.get("wear") or [None] * 3
                    if prev is not None:
                        for i in range(3):
                            if w[i] is not None and prev[i] is not None and 0 <= prev[i] - w[i] < 0.05:
                                drops[i].append((prev[i] - w[i]) * 100)    # новый комплект (рост) — не убыль
                    prev = w
                    if r.get("valid"):
                        for i, x in enumerate(t.get("temp") or []):
                            if i < 3 and x is not None:
                                temps[i].append(x)
                        if t.get("pressure") is not None:
                            pres.append(t["pressure"])
                out[c] = {"wear": [round(_mean(d), 3) if d else None for d in drops],
                          "temp": [round(_mean(x)) if x else None for x in temps],
                          "pres": round(_mean(pres)) if pres else None}
            return out
        pit = a.get("_pit")
        if not pit:
            return None
        wp, tires, n = pit
        out = {}
        for c in CORNERS:
            w = wp.get(c) or [None] * 3
            t = (tires.get(c) or {}).get("temp") or [None] * 3
            out[c] = {"wear": [round((100 - x) / n, 3) if x is not None else None for x in w],
                      "temp": [round(x) if x is not None else None for x in t], "pres": None}
        return out
