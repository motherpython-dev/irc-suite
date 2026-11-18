"""Проверка модуля «Телеметрия» без iRacing: кадры .ibt -> тот же код, что в программе.

    python tools/telemetry_replay.py "<файл.ibt>" [--out <папка>]

1. Кадры .ibt по одному подаются в TelemetryModule._on_frame (как от отвода
   SimClient на 60 Гц), дальше — Lap и сводка круга, как в живой езде.
   Время круга дописывается через секунду (60 кадров) после линии.
2. Тот же файл разбирается постобработкой (ibt.process) -> строки laps_ibt.
3. Время кругов сверяется с LapLastLapTime из самого .ibt (эталон).

Ничего не пишет в папку данных iRC: результат — в --out (по умолчанию во
временную папку).
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from suite.modules import telemetry as T            # noqa: E402
from suite.modules.telemetry import ibt as ibtmod   # noqa: E402
from suite.modules.telemetry.laps import ALL_CHANNELS, build_map, missing_groups  # noqa: E402


class _Log:
    def info(self, *a, **k): pass
    def exception(self, *a, **k):
        import traceback
        traceback.print_exc()


def reference(path: Path) -> dict[int, float]:
    """Эталон: первое новое значение LapLastLapTime после каждой смены
    LapCompleted (сим обновляет его через 1–3 с после линии)."""
    ib = ibtmod._open(path)
    try:
        lc = ib.get_all("LapCompleted")
        llt = ib.get_all("LapLastLapTime")
    finally:
        ib.close()
    out, prev = {}, None
    for i, c in enumerate(lc):
        if prev is not None and c > prev:
            v0 = llt[i]
            for j in range(i, min(i + 600, len(llt))):
                if llt[j] != v0 and llt[j] > 0:
                    out[int(c)] = round(float(llt[j]), 3)
                    break
        prev = c
    return out


def replay_live(path: Path, out: Path) -> list[dict]:
    """Кадры .ibt -> _on_frame модуля, как в живой езде."""
    ib = ibtmod._open(path)
    try:
        names = set(ib.var_headers_names)
        use = [n for n in ALL_CHANNELS if n in names]
        data = {n: ib.get_all(n) for n in use}
        total = int(ib._disk_header.session_record_count)
    finally:
        ib.close()
    m = T.TelemetryModule()
    m.ctx = type("Ctx", (), {"log": _Log(), "publish": lambda *a: None, "config": {}})()
    m.root = out
    m.map = build_map(names)
    m.missing = missing_groups(build_map(set(names) - {
        "LFrideHeight", "RFrideHeight", "LRrideHeight", "RRrideHeight", "CFSRrideHeight",
        "LFpressure", "RFpressure", "LRpressure", "RRpressure"}))     # как в live у этой машины
    folder = out / "live"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "laps.jsonl").unlink(missing_ok=True)          # прошлый прогон не дописываем
    m.sess = {"key": [0, "replay", "replay"], "dir": "live", "path": folder, "laps": 0,
              "started": time.time(), "car": "replay", "track": "replay", "subsession": 0,
              "sessions": [], "y": {}, "me": {}}
    m._index_update = lambda *a, **k: None
    m.trk = T.LapTracker(m._cfg, m.map)
    clock = {"t": 0.0}
    m._now = lambda: clock["t"]                             # часы модуля — по кадрам файла
    for i in range(total):
        v = {n: data[n][i] for n in use}
        clock["t"] = i / 60
        m._on_frame(v)
        m._flush_pending()                                  # как поток модуля, только на каждом кадре
    m._flush_pending(force=True)
    print(f"кадров пришло {m.frames_in}, в круги {m.frames_used}, отброшено {m.dropped}, ошибок {m.errors}")
    return [r for r in T._read_jsonl(folder / "laps.jsonl") if "lap" in r]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ibt", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    out = a.out or Path(tempfile.mkdtemp(prefix="telemetry-replay-"))
    out.mkdir(parents=True, exist_ok=True)

    ref = reference(a.ibt)
    t0 = time.monotonic()
    live = replay_live(a.ibt, out)
    t1 = time.monotonic()
    rows = ibtmod.process(a.ibt, dict(T.TelemetryModule.DEFAULTS))
    t2 = time.monotonic()
    (out / "laps_ibt.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")

    print(f"живой прогон: {len(live)} кругов за {t1 - t0:.1f} с; из .ibt: {len(rows)} кругов за {t2 - t1:.1f} с")
    print(f"{'круг':>4} {'эталон':>8} {'live':>8} {'ibt':>8}  valid  отметки            RH LF пов.  сплиттер   ход LF max")
    bad = 0
    by_live = {r["lap"]: r for r in live}
    for r in rows:
        n = r["lap"]
        e, lv = ref.get(n), by_live.get(n, {})
        lt, it = lv.get("lap_time"), r["lap_time"]
        ok = e is not None and it is not None and abs(e - it) <= 0.05 and (lt is None or abs(e - lt) <= 0.05)
        bad += 0 if (ok or e is None or e <= 0) else 1
        rh = r["ride_height"]
        print(f"{n:>4} {e if e is not None else '—':>8} {lt if lt is not None else '—':>8} {it if it is not None else '—':>8}"
              f"  {str(r['valid']):5}  {','.join(r['flags']) or '—':17}  {rh['LF']['corner_mean']}  "
              f"{rh['CFSR']['corner_mean']}  {r['shock_defl']['LF']['max']}{'' if ok else '   <-- расхождение'}")
    missing = [n for n in ref if n not in {r['lap'] for r in rows}]
    print(f"\nкругов в эталоне {len(ref)}, без строки: {missing or 'нет'}; расхождений по времени > 0.05 с: {bad}")
    print(f"результат: {out}")
    return 0 if not missing and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
