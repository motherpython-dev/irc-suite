"""`.ibt` iRacing: привязка к сессии и пересчёт кругов.

Привязка — по содержимому, а не по времени файла: в заголовке `.ibt` есть
номер сессии (`WeekendInfo.SubSessionID`) и время начала записи. По одному
времени изменения к сессии прилипали файлы предыдущей.

Пересчёт: в `.ibt` есть каналы, которых нет в живом потоке (высоты по
углам, сплиттер, горячие давления). Кадры файла прогоняются через тот же
`Lap`, что и живые, и пишутся в `laps_ibt.jsonl` — та же схема строки плюс
`source: "ibt"` и `ibt_file`. Читаются только нужные каналы (`get_all`).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .laps import ALL_CHANNELS, Lap, LapTracker, build_map, num

TICK_RATE = 60


def ibt_dirs() -> list[Path]:
    home = Path.home()
    return [p for p in (home / "Documents" / "iRacing" / "telemetry",
                        home / "OneDrive" / "Documents" / "iRacing" / "telemetry",
                        home / "OneDrive" / "Документы" / "iRacing" / "telemetry") if p.is_dir()]


def _open(path: Path):
    import irsdk
    ib = irsdk.IBT()
    ib.open(str(path))
    return ib


def _session_text(ib) -> str:
    h = ib._header
    raw = bytes(ib._shared_mem[h.session_info_offset:h.session_info_offset + h.session_info_len])
    return raw.rstrip(b"\x00").decode("latin-1", "replace")


def header(path: Path) -> dict | None:
    """Номер сессии, время начала записи и число кадров — без чтения кадров."""
    try:
        ib = _open(path)
    except Exception:                                    # noqa: BLE001
        return None
    try:
        text = _session_text(ib)
        sub = re.search(r"\bSubSessionID: (\d+)", text)
        dh = ib._disk_header
        start = float(dh.session_start_date or 0)
        return {"subsession": int(sub.group(1)) if sub else 0, "start": start,
                "records": int(dh.session_record_count or 0),
                "end": start + int(dh.session_record_count or 0) / TICK_RATE}
    except Exception:                                    # noqa: BLE001
        return None
    finally:
        ib.close()


def find_for_session(car_slug: str, subsession: int, opened: float, closed: float,
                     slug) -> list[dict]:
    """`.ibt` этой сессии: машина в имени, тот же номер сессии, запись начата
    между открытием сессии (−60 с) и её закрытием."""
    out = []
    for d in ibt_dirs():
        for f in d.glob("*.ibt"):
            if car_slug not in slug(f.name):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime < opened - 60:
                continue                                 # дописан до сессии — точно не наш
            h = header(f)
            if not h or h["subsession"] != int(subsession or 0):
                continue
            if not (opened - 60 <= h["start"] <= closed + 5):
                continue
            out.append({"path": str(f), "size": st.st_size, "start": h["start"],
                        "records": h["records"],
                        "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))})
    out.sort(key=lambda x: x["start"])
    return out


def process(path: Path, cfg: dict, setups: list[tuple[float, int]] | None = None) -> list[dict]:
    """Круги из `.ibt` тем же кодом, что живые. setups — [(время по часам, id)]
    снимков сетапа: номер сетапа кругу ставится по времени начала круга."""
    ib = _open(path)
    try:
        names = set(ib.var_headers_names or [])
        m = build_map(names)
        use = [n for n in ALL_CHANNELS if n in names]
        data = {n: ib.get_all(n) for n in use}
        start = float(ib._disk_header.session_start_date or 0)
        total = int(ib._disk_header.session_record_count or 0)
    finally:
        ib.close()
    setups = sorted(setups or [])

    def setup_at(wall: float):
        sid = None
        for t, i in setups:
            if t <= wall + 2:
                sid = i
        return sid

    rows: list[dict] = []
    trk = LapTracker(cfg, m)
    fname = Path(path).name
    v = {}
    for i in range(total):
        v = {n: data[n][i] for n in use}
        now = i / TICK_RATE
        for lap, no, t, sid in trk.ready(v.get("LapLastLapTime"), now):
            rows.append(_row(lap, no, t, sid, fname))
        if "IsOnTrack" in v and not v.get("IsOnTrack"):
            continue
        trk.feed(v, now, setup_at(start + now))
    for lap, no, t, sid in trk.ready(v.get("LapLastLapTime"), total / TICK_RATE, force=True):
        rows.append(_row(lap, no, t, sid, fname))
    return rows


def _row(lap: Lap, no: int, lap_time, setup_id, fname: str) -> dict:
    snum = lap.last.get("SessionNum")
    row = lap.summary(no, lap_time, setup_id, int(snum) if num(snum) is not None else None)
    row["source"] = "ibt"
    row["ibt_file"] = fname
    return row
