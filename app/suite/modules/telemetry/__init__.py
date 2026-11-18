"""Модуль «Телеметрия»: данные своей машины для разбора сетапа.

ТЗ — docs/TELEMETRY_SPEC.md (этап 1) и docs/TELEMETRY_FIXES.md (Б1–Б7):

  * дамп каналов и карта «логический канал -> имя в SDK» по машине;
  * снимки сетапа из данных сессии (`CarSetup`) — когда сетап изменился и
    держится 2 с; последние замеры шин из сетапа изменением не считаются;
  * сводка по каждому кругу (`laps.jsonl`) с номером сетапа;
  * после сессии — те же круги из `.ibt` (`laps_ibt.jsonl`), с высотами и
    давлениями, которых в живом потоке нет;
  * отправка сетапов и сводок на irchamps.gg (`/api/desktop/telemetry`) —
    в хранилище по машинам, на сайте их не видно; `.ibt` остаётся у человека.
    Согласие — сам включённый модуль.

Кадры приходят отводом SimClient (`add_tap`) на 60 Гц; модуль сам отбрасывает
повторы кадра, повтор (`IsReplayPlaying`) и кадры вне трассы (`IsOnTrack`) и
считает, сколько и почему отброшено, — поломку видно в `status()`.
Обработчик кадра только копит сводку круга; сессия, сетап, файлы — в потоке
модуля 4 раза в секунду; отправка на сайт — в своём потоке.

Раскладка: <папка данных>/telemetry/<дата>_<трасса>_<машина>_<сессия>/
session.json, setups/NNN.yaml + .meta.json, laps.jsonl, laps_ibt.jsonl,
ibt.json, upload.json; в корне — index.json (список сессий) и vars/.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ...core import config as core_config
from ...core.modules import Context
from ...core.version import VERSION
from . import ibt as ibtmod
from .laps import ALL_CHANNELS, LapTracker, build_map, missing_groups, num, rnd, tires

REUSE_S = 30 * 60          # сессия переоткрылась в течение 30 мин — та же папка (Б5)
SETUP_STABLE_S = 2.0       # снимок сетапа — только если он держится 2 с (Б7)
UPLOAD_EVERY_S = 20.0
UPLOAD_LAPS = 200          # строк кругов в одной отправке


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())[:40] or "x"


def _setup_core(v):
    """Сетап без замеров шин (`Last*`) и счётчика изменений: iRacing кладёт
    в раздел сетапа последние давления и температуры и меняет счётчик при
    каждом замере — это не смена сетапа."""
    if isinstance(v, dict):
        return {k: _setup_core(x) for k, x in v.items()
                if not str(k).startswith("Last") and k != "UpdateCount"}
    if isinstance(v, list):
        return [_setup_core(x) for x in v]
    return v


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _read_jsonl(p: Path) -> list[dict]:
    out = []
    try:
        for line in p.read_text("utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


IBT_KEEP_DAYS = 14          # свои .ibt (привязанные к сессиям iRC) — не дольше, потом удаляем
IBT_CLEAN_EVERY = 3600.0    # как часто проверять, с


class TelemetryModule:
    id = "telemetry"
    title = "Телеметрия"

    DEFAULTS = {"hz": 60, "corner_lat": 4.0, "straight_lat": 1.5, "straight_steer": 0.03,
                "auto_ibt": False, "upload": True}

    def __init__(self) -> None:
        self.ctx: Context | None = None
        self._tap = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.root: Path | None = None
        self.sess: dict | None = None
        self.trk: LapTracker | None = None        # нарезка кругов текущей сессии
        self._now = time.monotonic                 # часы кругов; офлайн-прогон подменяет
        self._last: dict = {}
        self._last_tick = None
        self._cfg = dict(self.DEFAULTS)
        # карта каналов этой машины (Б2)
        self.map: dict = build_map([])
        self.missing: list[str] = []
        self.vars: list[dict] = []
        self._vars_car = ""
        # сетап (Б7)
        self._setup_sha = ""
        self._cand_sha = ""
        self._cand_since = 0.0
        self.setup_id = 0
        # диагностика (Б1)
        self.frames_in = 0
        self.frames_used = 0
        self.dropped = {"not_live": 0, "not_on_track": 0, "no_session": 0, "same_tick": 0, "error": 0}
        self._last_frame = 0.0
        self.last_error = ""
        self._err_logged = 0.0
        self.errors = 0
        self.state = "выключен"
        self._stall_since = None
        self._tyre_avg = None                 # средний остаток резины по последнему замеру
        self._tyre_laps = 0                   # кругов на комплекте
        self._tyre_last = None                # последняя сводка резины — для вкладки
        self._stall_written = False
        self._on_track = False
        self._offline_since = None
        self.ibt_state = ""
        self._ibt_clean_at = 0.0               # когда последний раз чистили старые .ibt
        self.upload_state = ""
        self.uploaded = 0
        self.radar = None              # сбор данных для радара (radar_probe.py), по кнопке

    @property
    def cfg(self) -> dict:
        c = dict(self.DEFAULTS)
        if self.ctx:
            c.update({k: v for k, v in self.ctx.config.items() if k in self.DEFAULTS})
        return c

    # ---------- жизненный цикл ----------

    def start(self, ctx: Context) -> None:
        self.ctx = ctx
        self._cfg = self.cfg
        self.root = core_config.data_dir() / "telemetry"
        (self.root / "vars").mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._tap = ctx.sim.add_tap(ALL_CHANNELS, float(self._cfg["hz"]), self._on_frame)
        from .radar_probe import RadarProbe
        self.radar = RadarProbe(ctx.sim, self.root, ctx.log)
        self._threads = [threading.Thread(target=self._run, name="telemetry", daemon=True),
                         threading.Thread(target=self._uploader, name="telemetry-upload", daemon=True)]
        for t in self._threads:
            t.start()
        self.state = "ждёт выезда на трассу"

    def stop(self) -> None:
        if self.radar:
            self.radar.stop()
        if self.ctx and self._tap:
            self.ctx.sim.remove_tap(self._tap)
        self._tap = None
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)
        self._threads = []
        try:
            self._flush_pending(force=True)
            # разбор .ibt при закрытии программы не делаем — он затягивал выход на секунды
            # (перезапуск ради обновления спотыкался); разберём после следующего запуска
            self._close_session(background=False, reprocess=False)
        except Exception:                        # noqa: BLE001
            self._fail("остановка")
        self.state = "выключен"
        self.ctx = None

    # ---------- кадр 60 Гц (поток опроса сима) ----------

    def _on_frame(self, v: dict) -> None:
        self.frames_in += 1
        self._last_frame = time.monotonic()
        try:
            tick = v.get("SessionTick")
            if tick is not None and tick == self._last_tick:
                self.dropped["same_tick"] += 1
                return
            self._last_tick = tick
            self._last = v
            if v.get("IsReplayPlaying"):
                self.dropped["not_live"] += 1
                return
            if not v.get("IsOnTrack"):
                self.dropped["not_on_track"] += 1
                return
            with self._lock:
                if self.sess is None or self.trk is None:
                    self.dropped["no_session"] += 1
                    return
                # сетап — тот, на котором круг проехан: берётся на линии
                self.trk.feed(v, self._now(), self.setup_id)
            self.frames_used += 1
        except Exception:                        # noqa: BLE001
            self.dropped["error"] += 1
            self._fail("кадр")

    def _fail(self, what: str) -> None:
        """Ошибка не глушится: счётчик, текст для status, журнал не чаще раза в 10 с."""
        self.errors += 1
        self.last_error = f"{what}: {traceback.format_exc(limit=1).strip().splitlines()[-1]}"
        now = time.monotonic()
        if self.ctx and now - self._err_logged >= 10:
            self._err_logged = now
            self.ctx.log.exception("телеметрия: ошибка (%s)", what)

    # ---------- поток модуля ----------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._housekeeping()
            except Exception:                    # noqa: BLE001
                self._fail("поток модуля")
            self._stop.wait(0.25)

    def _housekeeping(self) -> None:
        if time.monotonic() - self._ibt_clean_at > IBT_CLEAN_EVERY:
            first = self._ibt_clean_at == 0.0
            self._ibt_clean_at = time.monotonic()
            threading.Thread(target=self._ibt_upkeep, args=(first,), name="telemetry-ibt-upkeep", daemon=True).start()
        sim = self.ctx.sim
        snap = sim.snapshot()
        if not snap.connected or sim.demo:
            if self.sess and self._offline_since is None:
                self._offline_since = time.monotonic()
            if self.sess and time.monotonic() - self._offline_since > 10:
                self._flush_pending(force=True)
                self._close_session()
            self.state = "демо-гонку не пишем" if sim.demo else "ждёт iRacing"
            return
        self._offline_since = None
        y = sim.session_yaml()
        di, w = y["DriverInfo"], y["WeekendInfo"]
        me = next((d for d in di.get("Drivers") or [] if d.get("CarIdx") == di.get("DriverCarIdx")), {})
        car = str(me.get("CarPath") or "")
        if not car:
            self.state = "ждёт данных сессии"
            return
        if car != self._vars_car:
            self._dump_vars(car)
        key = [int(w.get("SubSessionID") or 0), car, str(w.get("TrackID") or w.get("TrackName") or "")]
        if self.sess is None or self.sess["key"] != key:
            self._flush_pending(force=True)
            self._close_session()
            self._open_session(key, y, me)
        self._track_session_num(y)
        self._check_setup(sim.car_setup(), di)
        self._flush_pending()
        self._pit_stall()
        self._auto_ibt()
        age = time.monotonic() - self._last_frame if self._last_frame else None
        if age is None or age > 3:
            self.state = "кадры не приходят"
        elif self._last.get("IsReplayPlaying"):
            self.state = "повтор — не пишем"
        elif self._last.get("IsOnTrack") and self.trk and self.trk.lap:
            lc = self.trk.lc
            self.state = f"пишу · круг {lc + 1 if lc is not None and lc >= 0 else 1} · сетап {self.setup_id or '—'}"
        else:
            self.state = "в боксе · ждёт выезда"

    # ---------- сессия ----------

    def _index(self) -> list[dict]:
        try:
            return json.loads((self.root / "index.json").read_text("utf-8"))
        except (OSError, ValueError):
            return []

    def _open_session(self, key: list, y: dict, me: dict) -> None:
        w = y["WeekendInfo"]
        now = time.time()
        reuse = None
        for it in self._index():
            if it.get("key") == key and (self.root / it.get("dir", "")).is_dir():
                closed = it.get("closed_at")
                if closed is None or now - float(closed) <= REUSE_S:
                    reuse = it
                break
        if reuse:
            name = reuse["dir"]
            started, laps = float(reuse.get("started") or now), int(reuse.get("laps") or 0)
        else:
            name = (f"{datetime.now():%Y-%m-%d_%H%M}_{_slug(w.get('TrackName'))}_"
                    f"{_slug(me.get('CarPath'))}_{key[0] or 'test'}")
            started, laps = now, 0
        folder = self.root / name
        (folder / "setups").mkdir(parents=True, exist_ok=True)
        sessions = []
        try:
            sessions = json.loads((folder / "session.json").read_text("utf-8")).get("sessions") or []
        except (OSError, ValueError):
            pass
        with self._lock:
            self.sess = {"key": key, "dir": name, "path": folder, "laps": laps, "started": started,
                         "car": me.get("CarPath"), "track": w.get("TrackName"),
                         "subsession": key[0], "sessions": sessions, "y": y, "me": me}
            self.trk = LapTracker(self._cfg, self.map)
            self._tyre_avg, self._tyre_laps, self._tyre_last = None, 0, None
        # нумерация сетапов продолжается; повторно тот же сетап не пишем
        metas = sorted((folder / "setups").glob("*.meta.json"))
        self.setup_id = len(metas)
        self._setup_sha = ""
        if metas:
            try:
                self._setup_sha = json.loads(metas[-1].read_text("utf-8")).get("sha_setup") or ""
            except (OSError, ValueError):
                pass
        self._cand_sha = ""
        self._write_session()
        self._index_update()
        self._publish("telemetry.session", {"dir": name, "track": w.get("TrackDisplayName"),
                                            "car": me.get("CarScreenName"), "continued": bool(reuse)})
        self.ctx.log.info("телеметрия: сессия %s%s", name, " (продолжение)" if reuse else "")

    def _track_session_num(self, y: dict) -> None:
        """Номер и тип сессии — по кадрам; внутри одной сессии iRacing бывают
        практика -> квала -> гонка (Б6)."""
        s = self.sess
        snum = self._last.get("SessionNum")
        if not s or snum is None:
            return
        snum = int(snum)
        if any(x.get("session_num") == snum for x in s["sessions"]):
            return
        stype = ""
        for x in y["SessionInfo"].get("Sessions") or []:
            if x.get("SessionNum") == snum:
                stype = x.get("SessionType") or ""
        s["sessions"].append({"session_num": snum, "session_type": stype,
                              "first_seen": datetime.now().isoformat(timespec="seconds")})
        s["y"] = y
        self._write_session()

    def _write_session(self) -> None:
        s = self.sess
        w, me = s["y"]["WeekendInfo"], s["me"]
        cur = s["sessions"][-1] if s["sessions"] else {}
        data = {
            "subsession_id": w.get("SubSessionID"),
            "session_num": cur.get("session_num"), "session_type": cur.get("session_type"),
            "sessions": s["sessions"],
            "track": {k: w.get(k) for k in ("TrackName", "TrackDisplayName", "TrackConfigName",
                                             "TrackLength", "TrackID")},
            "car": {"CarPath": me.get("CarPath"), "CarScreenName": me.get("CarScreenName")},
            "driver": {"UserID": me.get("UserID"), "UserName": me.get("UserName")},
            "weather": {k: w.get(k) for k in ("TrackSurfaceTemp", "TrackAirTemp", "TrackWindVel",
                                               "TrackWindDir", "TrackSkies", "TrackRelativeHumidity",
                                               "TrackAirPressure")},
            "started_at": datetime.fromtimestamp(s["started"]).isoformat(timespec="seconds"),
            "irc_version": VERSION,
        }
        (s["path"] / "session.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    def _close_session(self, background: bool = True, reprocess: bool = True) -> None:
        s = self.sess
        if not s:
            return
        with self._lock:
            self.sess = None
            self.trk = None
        try:
            files = self._register_ibt(s)
            self._index_update(s, closed=True)
            if files and reprocess:
                if background:
                    threading.Thread(target=self._reprocess, args=(s["path"],),
                                     name="telemetry-ibt", daemon=True).start()
                else:
                    self._reprocess(s["path"])
        except Exception:                        # noqa: BLE001
            self._fail("закрытие сессии")

    def _index_update(self, s: dict | None = None, closed: bool = False) -> None:
        s = s or self.sess
        items = self._index()
        if s:
            items = [i for i in items if i.get("dir") != s["dir"]]
            items.insert(0, {"dir": s["dir"], "key": s["key"], "track": s["track"], "car": s["car"],
                             "subsession": s["subsession"], "laps": s["laps"], "started": s["started"],
                             "started_at": datetime.fromtimestamp(s["started"]).isoformat(timespec="seconds"),
                             "closed_at": time.time() if closed else None})
        (self.root / "index.json").write_text(json.dumps(items[:500], ensure_ascii=False, indent=1), "utf-8")

    # ---------- каналы и сетап ----------

    def _dump_vars(self, car: str) -> None:
        heads = self.ctx.sim.var_headers()
        if not heads:
            return
        self.vars = heads
        self._vars_car = car
        self.map = build_map(h["name"] for h in heads)
        self.missing = missing_groups(self.map)
        (self.root / "vars" / f"{_slug(car)}_live.json").write_text(
            json.dumps({"car": car, "saved_at": datetime.now().isoformat(timespec="seconds"),
                        "mapping": self.map, "missing_live": self.missing, "vars": heads},
                       ensure_ascii=False, indent=1), "utf-8")
        if self.missing:
            self.ctx.log.info("телеметрия: в live нет %s — возьмём из .ibt", ", ".join(self.missing))

    def _check_setup(self, cs: dict | None, di: dict) -> None:
        if not cs or not self.sess:
            return
        import yaml
        core = json.dumps(_setup_core(cs), sort_keys=True, ensure_ascii=False, default=str)
        sha = hashlib.sha1(core.encode("utf-8")).hexdigest()
        if sha == self._setup_sha:
            self._cand_sha = ""
            return
        now = time.monotonic()
        if sha != self._cand_sha:                # сетап только что поменялся — ждём, пока устоится
            self._cand_sha, self._cand_since = sha, now
            return
        if now - self._cand_since < SETUP_STABLE_S:
            return
        text = yaml.safe_dump(cs, allow_unicode=True, sort_keys=False)
        sdir = self.sess["path"] / "setups"
        self.setup_id = len(list(sdir.glob("*.meta.json"))) + 1
        self._setup_sha, self._cand_sha = sha, ""
        nnn = f"{self.setup_id:03d}"
        (sdir / f"{nnn}.yaml").write_text(text, "utf-8")
        meta = {"setup_id": self.setup_id, "update_count": cs.get("UpdateCount"),
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "session_time": rnd(num(self._last.get("SessionTime")), 3),
                "sha1": hashlib.sha1(text.encode("utf-8")).hexdigest(), "sha_setup": sha,
                "setup_name": di.get("DriverSetupName"),
                "load_type": di.get("DriverSetupLoadTypeName"),
                "is_modified": di.get("DriverSetupIsModified")}
        (sdir / f"{nnn}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
        self._publish("telemetry.setup", meta)

    # ---------- круги ----------

    def _flush_pending(self, force: bool = False) -> None:
        """Дописать круги, у которых сим уже обновил время (или вышло ожидание)."""
        if not self.sess or not self.trk:
            return
        with self._lock:
            ready = self.trk.ready(self._last.get("LapLastLapTime"), self._now(), force)
        for lap, no, lap_time, sid in ready:
            snum = lap.last.get("SessionNum")
            row = lap.summary(no, lap_time, sid or None,
                              int(snum) if snum is not None else None, self.missing)
            self._tyre_stint(row)
            self._append(row)
            self.sess["laps"] += 1
            self._index_update()
            self._publish("telemetry.lap", row)

    def _tyre_stint(self, row: dict) -> None:
        """Круги на комплекте: износ в живом потоке обновляется только в
        боксе — новый комплект виден по росту остатка резины."""
        ws = [w for c in (row.get("tires") or {}).values() for w in (c.get("wear") or []) if w is not None]
        avg = sum(ws) / len(ws) if ws else None
        prev = self._tyre_avg
        if avg is not None and prev is not None and avg > prev + 0.03:
            self._tyre_laps = 0
            row["tyres_new"] = True
        if avg is not None:
            self._tyre_avg = avg
        self._tyre_laps += 1
        row["tyre_laps"] = self._tyre_laps
        self._tyre_last = {"tires": row.get("tires"), "wear_pct": row.get("tire_wear_pct"),
                           "laps": self._tyre_laps, "track_temp": row.get("track_temp"),
                           "g": row.get("g"), "lap": row.get("lap")}

    def _append(self, row: dict, name: str = "laps.jsonl") -> None:
        with open(self.sess["path"] / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _pit_stall(self) -> None:
        v = self._last
        if v.get("PlayerCarInPitStall"):
            if self._stall_since is None:
                self._stall_since = time.monotonic()
            elif not self._stall_written and time.monotonic() - self._stall_since > 2 and self.sess:
                self._append({"event": "pit_stall", "session_time": rnd(num(v.get("SessionTime")), 3),
                              "setup_id": self.setup_id or None, "tires": tires(v, self.map)})
                self._stall_written = True
        else:
            self._stall_since, self._stall_written = None, False

    # ---------- .ibt ----------

    def _auto_ibt(self) -> None:
        on = bool(self._last.get("IsOnTrack"))
        if on == self._on_track:
            return
        self._on_track = on
        if not self.cfg["auto_ibt"]:
            return
        try:
            import irsdk
            mode = irsdk.TelemCommandMode.start if on else irsdk.TelemCommandMode.stop
            self.ctx.rc.ir.telem_command(mode)
            self.ctx.log.info("телеметрия: запись .ibt %s", "включена" if on else "выключена")
        except Exception:                        # noqa: BLE001
            self._fail("запись .ibt")

    def _ibt_upkeep(self, first: bool) -> None:
        if first:
            self._reprocess_pending()
        self._clean_ibt()

    def _reprocess_pending(self, days: float = 3.0) -> None:
        """После запуска: недавние сессии, у которых .ibt новее разбора (программу
        закрыли до разбора), — разобрать в фоне."""
        if not self.root:
            return
        cut = time.time() - days * 86400
        try:
            folders = [f for f in self.root.iterdir() if (f / "ibt.json").is_file()]
        except OSError:
            return
        for folder in folders:
            try:
                info = json.loads((folder / "ibt.json").read_text("utf-8"))
                paths = [Path(str(f.get("path") or "")) for f in info.get("files") or [info]]
                newest = max((p.stat().st_mtime for p in paths if p.is_file()), default=0)
                if newest < cut:
                    continue
                done = folder / "laps_ibt.jsonl"
                if done.is_file() and done.stat().st_mtime >= newest:
                    continue
                if self.sess and self.sess.get("path") == folder:
                    continue                         # сессия ещё идёт — разберётся при закрытии
                self._reprocess(folder)
            except Exception:                        # noqa: BLE001
                self._fail(f"разбор после запуска {folder.name}")

    def _clean_ibt(self) -> None:
        """Свои .ibt старше IBT_KEEP_DAYS — удалить. Свои — те, что привязаны к сессиям
        iRC (ibt.json в папке сессии); чужих файлов в папке iRacing не трогаем. Не
        разобранный файл сначала разбираем: круги из .ibt (горячие давления, высоты)
        остаются в laps_ibt.jsonl."""
        if not self.root:
            return
        cut = time.time() - IBT_KEEP_DAYS * 86400
        removed, freed = 0, 0
        try:
            folders = [f for f in self.root.iterdir() if (f / "ibt.json").is_file()]
        except OSError:
            return
        for folder in folders:
            try:
                info = json.loads((folder / "ibt.json").read_text("utf-8"))
            except (OSError, ValueError):
                continue
            old = []
            for f in info.get("files") or [info]:
                path = Path(str(f.get("path") or ""))
                try:
                    if path.is_file() and path.suffix.lower() == ".ibt" and path.stat().st_mtime < cut:
                        old.append(path)
                except OSError:
                    pass
            if not old:
                continue
            if not (folder / "laps_ibt.jsonl").is_file():
                try:
                    self._reprocess(folder)          # разбор до удаления — данные не теряются
                except Exception:                    # noqa: BLE001
                    self._fail(f"разбор перед удалением {folder.name}")
                    continue
            for path in old:
                try:
                    size = path.stat().st_size
                    path.unlink()
                    removed, freed = removed + 1, freed + size
                except OSError:
                    pass
        if removed and self.ctx:
            self.ctx.log.info("телеметрия: удалено старых .ibt (старше %d дней): %d, %.1f ГБ",
                              IBT_KEEP_DAYS, removed, freed / 1e9)

    def _register_ibt(self, s: dict) -> list[dict]:
        """Свои `.ibt`: по номеру сессии внутри файла и времени начала записи (Б4)."""
        files = ibtmod.find_for_session(_slug(s.get("car")), s["subsession"], s["started"], time.time(), _slug)
        if files:
            (s["path"] / "ibt.json").write_text(
                json.dumps({**files[-1], "files": files}, ensure_ascii=False, indent=2), "utf-8")
        return files

    def _reprocess(self, folder: Path) -> dict:
        """Круги из `.ibt` -> laps_ibt.jsonl (Б3). Долго — вызывать в фоне."""
        try:
            info = json.loads((folder / "ibt.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "error": "no_ibt", "message": "У сессии нет .ibt"}
        setups = []
        for m in sorted((folder / "setups").glob("*.meta.json")):
            try:
                j = json.loads(m.read_text("utf-8"))
                setups.append((_ts(j.get("saved_at")), int(j.get("setup_id") or 0)))
            except (OSError, ValueError):
                pass
        rows = []
        t0 = time.monotonic()
        for f in info.get("files") or [info]:
            p = Path(f["path"])
            if not p.is_file():
                continue
            self.ibt_state = f"разбираю {p.name}"
            try:
                rows += ibtmod.process(p, self._cfg, setups)
            except Exception:                    # noqa: BLE001
                self._fail(f"разбор {p.name}")
        tmp = folder / "laps_ibt.jsonl.tmp"
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
        os.replace(tmp, folder / "laps_ibt.jsonl")
        self.ibt_state = f"из .ibt: {len(rows)} кругов за {time.monotonic() - t0:.0f} с"
        if self.ctx:
            self.ctx.log.info("телеметрия: %s (%s)", self.ibt_state, folder.name)
        return {"ok": True, "laps": len(rows)}

    # ---------- отправка на irchamps.gg ----------

    def _uploader(self) -> None:
        while not self._stop.wait(UPLOAD_EVERY_S):
            try:
                self._upload_once()
            except Exception:                    # noqa: BLE001
                self._fail("отправка")

    def _upload_once(self) -> None:
        if not self.cfg["upload"]:
            self.upload_state = "отправка выключена"
            return
        app = getattr(self.ctx, "app", None) if self.ctx else None
        lic = getattr(app, "license", None)
        if not lic or not lic.state.get("key"):
            self.upload_state = "нет входа — данные ждут"
            return
        for it in self._index()[:20]:
            folder = self.root / it.get("dir", "")
            if not folder.is_dir():
                continue
            while True:
                body, mark = self._upload_batch(folder)
                if not body:
                    break
                res = lic.api("/api/desktop/telemetry", body)
                if not res.get("ok"):
                    self.upload_state = ("нет связи с сайтом — отправим позже" if res.get("error") == "offline"
                                         else f"сайт не принял: {res.get('error') or res.get('message')}")
                    return
                (folder / "upload.json").write_text(json.dumps(mark), "utf-8")
                self.uploaded += len(body.get("laps") or []) + len(body.get("laps_ibt") or [])
        self.upload_state = "всё отправлено"

    def _upload_batch(self, folder: Path) -> tuple[dict | None, dict]:
        """Что ещё не отправлено из этой сессии: сетапы, строки кругов, данные сессии."""
        try:
            sent = json.loads((folder / "upload.json").read_text("utf-8"))
        except (OSError, ValueError):
            sent = {}
        mark = {"setups": list(sent.get("setups") or []), "laps": int(sent.get("laps") or 0),
                "laps_ibt": int(sent.get("laps_ibt") or 0), "session": sent.get("session") or ""}
        try:
            session = json.loads((folder / "session.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None, mark
        body = {"dir": folder.name, "car": (session.get("car") or {}).get("CarPath") or "",
                "session": session, "setups": [], "laps": [], "laps_ibt": []}
        s_sha = hashlib.sha1(json.dumps(session, sort_keys=True).encode()).hexdigest()
        changed = s_sha != mark["session"]
        for meta in sorted((folder / "setups").glob("*.meta.json")):
            try:
                j = json.loads(meta.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            sid = int(j.get("setup_id") or 0)
            if sid in mark["setups"]:
                continue
            y = folder / "setups" / f"{sid:03d}.yaml"
            body["setups"].append({"id": sid, "meta": j,
                                   "yaml": y.read_text("utf-8") if y.is_file() else ""})
            mark["setups"].append(sid)
        laps = _read_jsonl(folder / "laps.jsonl")
        body["laps"] = laps[mark["laps"]:mark["laps"] + UPLOAD_LAPS]
        body["laps_offset"] = mark["laps"]          # сайт пропустит уже принятое при повторе
        mark["laps"] += len(body["laps"])
        # laps_ibt пересчитывается целиком — шлём заново, если строк стало больше
        ibt_rows = _read_jsonl(folder / "laps_ibt.jsonl")
        if len(ibt_rows) != mark["laps_ibt"]:
            body["laps_ibt"], body["laps_ibt_replace"] = ibt_rows, True
            mark["laps_ibt"] = len(ibt_rows)
        mark["session"] = s_sha
        if not (changed or body["setups"] or body["laps"] or body["laps_ibt"]):
            return None, mark
        return body, mark

    # ---------- наружу ----------

    def _publish(self, event: str, data: dict) -> None:
        try:
            self.ctx.publish(event, data)
        except Exception:                        # noqa: BLE001
            pass

    def status(self) -> dict:
        s = self.sess
        age = round(time.monotonic() - self._last_frame, 1) if self._last_frame else None
        snap = self.ctx.sim.snapshot() if self.ctx else None
        return {
            "state": self.state,
            "session": s["dir"] if s else None,
            "laps": s["laps"] if s else 0,
            "lap": (self.trk.lc + 1) if (s and self.trk and self.trk.lc is not None) else None,
            "setup_id": self.setup_id or None,
            "ibt_logging": bool(self._last.get("IsDiskLoggingActive")),
            "auto_ibt": bool(self.cfg["auto_ibt"]),
            "vars": len(self.vars),
            "errors": self.errors,
            "frames_in": self.frames_in, "frames_used": self.frames_used,
            "dropped": dict(self.dropped),
            "last_frame_age_s": age,
            "on_track": bool(self._last.get("IsOnTrack")),
            "sim_mode": snap.mode if snap else None,
            "connected": bool(snap and snap.connected),
            "last_error": self.last_error,
            "mapping": self.map, "missing_live": self.missing,
            "ibt_state": self.ibt_state,
            "tyres": self._tyre_last,
            "upload": bool(self.cfg["upload"]), "upload_state": self.upload_state, "uploaded": self.uploaded,
            "root": str(self.root) if self.root else str(core_config.data_dir() / "telemetry"),
        }

    def _session_dir(self, name: str | None) -> Path | None:
        if not self.root:
            self.root = core_config.data_dir() / "telemetry"
        if not name:
            if self.sess:
                return self.sess["path"]
            items = self._index()
            name = items[0].get("dir") if items else None
        if not name or "/" in name or "\\" in name or ".." in name:
            return None
        p = self.root / name
        return p if p.is_dir() else None

    def laps(self, session: str | None, since: int, source: str | None = None) -> dict:
        d = self._session_dir(session)
        if not d:
            return {"ok": False, "error": "not_found", "laps": []}
        if source not in ("live", "ibt"):
            source = "ibt" if (d / "laps_ibt.jsonl").is_file() else "live"
        rows, events = [], []
        for r in _read_jsonl(d / ("laps_ibt.jsonl" if source == "ibt" else "laps.jsonl")):
            if "event" in r:
                events.append(r)
            elif int(r.get("lap") or 0) > since:
                rows.append(r)
        return {"ok": True, "session": d.name, "source": source, "laps": rows, "events": events}

    def reprocess(self, session: str | None) -> dict:
        d = self._session_dir(session)
        if not d:
            return {"ok": False, "error": "not_found"}
        if not (d / "ibt.json").is_file():
            s = self.sess if self.sess and self.sess["path"] == d else None
            if not s:
                return {"ok": False, "error": "no_ibt", "message": "У сессии нет .ibt"}
            if not self._register_ibt(s):
                return {"ok": False, "error": "no_ibt", "message": "Своих .ibt для этой сессии не нашлось"}
        threading.Thread(target=self._reprocess, args=(d,), name="telemetry-ibt", daemon=True).start()
        self.ibt_state = "разбор .ibt запущен"
        return {"ok": True, "started": True}

    def setup(self, session: str | None, sid: int) -> dict:
        d = self._session_dir(session)
        if not d:
            return {"ok": False, "error": "not_found"}
        f = d / "setups" / f"{sid:03d}.yaml"
        if not f.is_file():
            return {"ok": False, "error": "not_found"}
        meta = d / "setups" / f"{sid:03d}.meta.json"
        return {"ok": True, "id": sid, "yaml": f.read_text("utf-8"),
                "meta": json.loads(meta.read_text("utf-8")) if meta.is_file() else None}

    def sessions(self) -> list:
        if not self.root:
            self.root = core_config.data_dir() / "telemetry"
        return self._index()

    def vars_dump(self) -> dict:
        if self.vars:
            return {"car": self._vars_car, "mapping": self.map, "missing_live": self.missing,
                    "vars": self.vars}
        files = sorted((core_config.data_dir() / "telemetry" / "vars").glob("*_live.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        return json.loads(files[0].read_text("utf-8")) if files else {"car": None, "vars": []}

    def set_settings(self, body: dict) -> dict:
        if self.ctx:
            for k in ("auto_ibt", "upload"):
                if k in body:
                    self.ctx.config[k] = bool(body[k])
            self.ctx.save_config()
        return {"ok": True, **self.status()}

    def radar_probe(self, body: dict | None = None) -> dict:
        """Сбор данных для радара: вкл/выкл и сводка порогов «машина рядом»."""
        if not self.radar:
            return {"ok": False, "error": "module_off", "message": "Включите модуль телеметрии."}
        if body and "on" in body:
            self.radar.start() if body["on"] else self.radar.stop()
        return {"ok": True, **self.radar.summary()}

    def open_folder(self) -> dict:
        p = self.root or core_config.data_dir() / "telemetry"
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(str(p))                     # noqa: S606
        return {"ok": True}

    def routes(self) -> dict:
        def local(fn):
            # данные своей машины — только этому компьютеру, не в сеть
            def h(r):
                if r.client_address[0] != "127.0.0.1":
                    return {"ok": False, "error": "forbidden"}
                return fn(r)
            return h

        def q(r) -> dict:
            return {k: v[0] for k, v in parse_qs(urlparse(r.path).query).items()}

        def body(r) -> dict:
            n = int(r.headers.get("Content-Length") or 0)
            try:
                return json.loads(r.rfile.read(n).decode("utf-8")) if n else {}
            except Exception:                    # noqa: BLE001
                return {}

        def to_int(s, d=0):
            try:
                return int(s)
            except (TypeError, ValueError):
                return d

        return {
            "GET /api/telemetry/status": local(lambda r: self.status()),
            "GET /api/telemetry/vars": local(lambda r: self.vars_dump()),
            "GET /api/telemetry/sessions": local(lambda r: self.sessions()),
            "GET /api/telemetry/laps": local(lambda r: self.laps(
                q(r).get("session"), to_int(q(r).get("since")), q(r).get("source"))),
            "GET /api/telemetry/setup": local(lambda r: self.setup(q(r).get("session"), to_int(q(r).get("id"), 1))),
            "POST /api/telemetry/reprocess": local(lambda r: self.reprocess(q(r).get("session"))),
            "POST /api/telemetry/settings": local(lambda r: self.set_settings(body(r))),
            "POST /api/telemetry/open": local(lambda r: self.open_folder()),
            "GET /api/telemetry/radar": local(lambda r: self.radar_probe()),
            "POST /api/telemetry/radar": local(lambda r: self.radar_probe(body(r))),
        }
