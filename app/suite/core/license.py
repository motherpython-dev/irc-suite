"""Допуск: программа работает у своих.

iRC — инструмент лиги, а не открытая утилита. Работать она должна только у
того, кто зарегистрирован на irchamps.gg и привязал там аккаунт iRacing:
по номеру аккаунта видно, кто на самом деле сидит за симом.

Пароль сайта в программу не вводится. Программа показывает короткий код,
человек подтверждает его на сайте под своим входом, и программа получает
ключ установки. Ключ лежит в `<данные>/license.json` вместе с последней
удачной проверкой.

Три правила, по которым принимается решение:

* **нет ключа** — программа заблокирована, показывается экран привязки;
* **сайт ответил** — верим ему: доступ есть, пока есть привязка iRacing и
  установку не отозвали в личном кабинете;
* **сайта нет** — работаем на последней удачной проверке ещё неделю. Гонка
  не должна срываться из-за упавшего сайта или пропавшего интернета.

Отдельно сверяется сим: если iRacing запущен и в нём другой аккаунт, чем
привязан на сайте, программа блокируется — ключ не передашь товарищу.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import urllib.parse
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger("license")

SITE = os.environ.get("IRC_SITE") or "https://irchamps.gg"
# Допуск включён: без входа модули стоят, правая часть панели затемнена и
# просит войти. Выключить для проверок — IRC_LICENSE=0 (docs/TECHNICAL.md, 10.3).
ENFORCE = os.environ.get("IRC_LICENSE", "1") != "0"
GRACE_DAYS = 7
GRACE_S = GRACE_DAYS * 24 * 3600
CHECK_EVERY_S = 3600
TIMEOUT = 10

# причина -> что видит человек
MESSAGES = {
    "not_linked": "Программа не подключена к аккаунту на irchamps.gg.",
    "no_iracing": "К аккаунту не привязан iRacing. Привяжите его на сайте, в настройках.",
    "not_confirmed": "Аккаунт на сайте не подтверждён.",
    "revoked": "Эта установка отозвана в личном кабинете.",
    "unknown_key": "Ключ установки недействителен — подключите программу заново.",
    "no_user": "Аккаунт не найден на сайте.",
    "offline_expired": f"Сайт недоступен больше {GRACE_DAYS} дней — подключитесь к интернету.",
    "other_account": "В iRacing другой аккаунт, не тот, что привязан на сайте.",
    "ok": "",
}


def _now() -> float:
    return time.time()


class License:
    """Состояние допуска и вся работа с сайтом."""

    def __init__(self, data_dir: Path, app_version: str = "", sim=None) -> None:
        self.path = Path(data_dir) / "license.json"
        self.app_version = app_version
        self.sim = sim
        self._lock = threading.RLock()
        self.state = self._load()
        self.pairing: dict = {}          # текущий код привязки, в файл не пишется
        self.login: dict = {}            # начатый вход через браузер: state и секрет PKCE
        self.last_error = ""

    # ---------- хранение ----------

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if not data.get("device_id"):
            data["device_id"] = uuid.uuid4().hex
            self.state = data
            self._save(data)
        return data

    def _save(self, data: dict | None = None) -> None:
        data = self.state if data is None else data
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(self.path)
        except OSError:
            log.warning("не удалось сохранить license.json")

    # ---------- сеть ----------

    def _post(self, path: str, body: dict | None = None, key: str = "") -> dict:
        req = urllib.request.Request(
            SITE.rstrip("/") + path,
            data=json.dumps(body or {}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": f"iRC/{self.app_version}",
                     **({"X-IRC-Key": key} if key else {})},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 401 — ответ по существу: ключ не годится. Разбираем его как ответ.
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:                      # noqa: BLE001
                return {"ok": False, "error": f"http_{e.code}"}
        except (urllib.error.URLError, socket.timeout, ValueError, OSError) as e:
            return {"ok": False, "error": "offline", "detail": str(e)}

    def api(self, path: str, body: dict | None = None) -> dict:
        """Запрос к сайту от имени этой установки (ключ X-IRC-Key)."""
        key = self.state.get("key")
        if not key:
            return {"ok": False, "error": "not_linked"}
        return self._post(path, body or {}, key=key)

    # ---------- привязка ----------

    def start_pair(self) -> dict:
        res = self._post("/api/desktop/pair/start", {
            "device_id": self.state["device_id"],
            "device_name": socket.gethostname()[:120],
            "app_version": self.app_version})
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "offline",
                    "message": "Сайт недоступен — проверьте интернет."
                               if res.get("error") == "offline"
                               else "Не удалось получить код."}
        self.pairing = {"code": res["code"], "url": res.get("verify_url") or SITE + "/desktop",
                        "until": _now() + int(res.get("expires_in") or 600)}
        return {"ok": True, **self.pairing}

    def poll_pair(self) -> dict:
        """Подтвердили ли код на сайте. Ключ сайт отдаёт ровно один раз."""
        if not self.pairing:
            return {"ok": False, "error": "no_pairing"}
        res = self._post("/api/desktop/pair/poll", {
            "device_id": self.state["device_id"], "code": self.pairing["code"]})
        status = res.get("status")
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "offline"}
        if status != "approved":
            if status in ("expired", "unknown") and _now() > self.pairing.get("until", 0):
                self.pairing = {}
            return {"ok": True, "status": status or "pending"}
        with self._lock:
            if res.get("key"):
                self.state["key"] = res["key"]
            self.state["user"] = res.get("user") or {}
            self.state["reason"] = res.get("reason") or "ok"
            self.state["allowed"] = bool(res.get("allowed"))
            self.state["last_ok"] = _now() if res.get("allowed") else 0
            self._save()
        self.pairing = {}
        log.info("программа подключена к аккаунту %s",
                 (self.state.get("user") or {}).get("username"))
        return {"ok": True, "status": "approved", **self.status()}

    # ---------- вход через сайт ----------

    def start_login(self, port: int) -> dict:
        """Адрес входа на сайте. Паролей на сайте нет (вход через Discord),
        поэтому логин идёт в браузере, а программа ждёт возврата на
        127.0.0.1:<port>/auth/callback.

        state защищает от чужого возврата, секрет PKCE — от перехвата кода:
        обменять код на ключ может только тот, кто знает секрет.
        """
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        state = secrets.token_urlsafe(24)
        query = urllib.parse.urlencode({
            "port": int(port), "state": state, "challenge": challenge,
            "device_id": self.state["device_id"],
            "device_name": socket.gethostname()[:120],
            "app_version": self.app_version})
        url = SITE.rstrip("/") + "/desktop/login?" + query
        self.login = {"state": state, "verifier": verifier, "url": url,
                      "until": _now() + 15 * 60}
        return {"ok": True, "url": url}

    def finish_login(self, code: str, state: str) -> dict:
        """Сайт вернул человека с кодом — обменять его на ключ установки."""
        pend = self.login
        if not pend or _now() > pend.get("until", 0):
            self.login = {}
            return {"ok": False, "error": "no_login",
                    "message": "Вход устарел — нажмите «Войти» в программе ещё раз."}
        if not code or not secrets.compare_digest(str(state or ""), pend["state"]):
            return {"ok": False, "error": "bad_state",
                    "message": "Ссылка входа не от этой программы."}
        self.login = {}
        res = self._post("/api/desktop/token", {
            "code": code, "verifier": pend["verifier"],
            "device_id": self.state["device_id"]})
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "offline",
                    "message": "Сайт недоступен — проверьте интернет."
                               if res.get("error") == "offline"
                               else "Вход не удался — попробуйте ещё раз."}
        self._accept(res)
        log.info("вход в программу: %s", (self.state.get("user") or {}).get("username"))
        return {"ok": True, **self.status()}

    def _accept(self, res: dict) -> None:
        """Сохранить ключ и ответ сайта — после входа или кода привязки."""
        with self._lock:
            if res.get("key"):
                self.state["key"] = res["key"]
            self.state["user"] = res.get("user") or {}
            self.state["reason"] = res.get("reason") or "ok"
            self.state["allowed"] = bool(res.get("allowed"))
            self.state["last_ok"] = _now() if res.get("allowed") else 0
            self._save()

    def browser_url(self) -> str:
        """Что открыть в браузере с экрана входа: начатый вход, код, настройки."""
        if self.login:
            return self.login["url"]
        if self.pairing:
            return self.pairing["url"]
        if self.state.get("reason") in ("no_iracing", "not_confirmed"):
            return SITE.rstrip("/") + "/settings"
        return SITE.rstrip("/") + "/desktop"

    def unlink(self) -> dict:
        """Отвязать эту установку. Ключ сайту больше не нужен."""
        key = self.state.get("key")
        if key:
            self._post("/api/desktop/unlink", {}, key=key)
        with self._lock:
            for k in ("key", "user", "allowed", "reason", "last_ok"):
                self.state.pop(k, None)
            self._save()
        return {"ok": True, **self.status()}

    # ---------- проверка ----------

    def check(self) -> dict:
        """Спросить сайт, можно ли работать. Молчание сайта — не отказ."""
        key = self.state.get("key")
        if not key:
            return {"ok": True, **self.status()}
        res = self._post("/api/desktop/verify", {"app_version": self.app_version}, key=key)
        with self._lock:
            if res.get("ok"):
                self.state["allowed"] = bool(res.get("allowed"))
                self.state["reason"] = res.get("reason") or "ok"
                self.state["user"] = res.get("user") or self.state.get("user") or {}
                if res.get("allowed"):
                    self.state["last_ok"] = _now()
                self.last_error = ""
            elif res.get("error") in ("unknown_key", "revoked", "no_key"):
                # сайт ответил по существу: ключ больше не годится
                self.state["allowed"] = False
                self.state["reason"] = res["error"]
                self.state.pop("key", None)
                self.last_error = ""
            else:
                self.last_error = str(res.get("error") or "offline")
            self._save()
        return {"ok": True, **self.status()}

    # ---------- решение ----------

    def sim_cust_id(self) -> int:
        """Номер аккаунта iRacing, под которым сейчас запущен сим."""
        if self.sim is None:
            return 0
        try:
            s = self.sim.snapshot()
            if not s.connected or s.player_car_idx < 0:
                return 0
            for d in s.drivers:
                if d.car_idx == s.player_car_idx:
                    return int(d.user_id or 0)
        except Exception:                          # noqa: BLE001
            return 0
        return 0

    def decide(self) -> tuple[bool, str]:
        if not ENFORCE:
            return True, "ok"
        st = self.state
        if not st.get("key"):
            # ключ мог отвалиться по ответу сайта — причину человеку показываем ту
            was = st.get("reason")
            return False, was if was in ("revoked", "unknown_key") else "not_linked"
        reason = st.get("reason") or "ok"
        if not st.get("allowed"):
            return False, reason if reason != "ok" else "not_linked"
        # сверка с симом: ключ не должен работать у другого человека
        linked = int((st.get("user") or {}).get("iracing_customer_id") or 0)
        mine = self.sim_cust_id()
        if linked and mine and mine != linked:
            return False, "other_account"
        left = _now() - float(st.get("last_ok") or 0)
        if left > GRACE_S:
            return False, "offline_expired"
        return True, "ok"

    def allowed(self) -> bool:
        return self.decide()[0]

    def status(self) -> dict:
        allowed, reason = self.decide()
        last_ok = float(self.state.get("last_ok") or 0)
        grace_left = max(0.0, GRACE_S - (_now() - last_ok)) if last_ok else 0.0
        user = self.state.get("user") or {}
        return {
            "allowed": allowed,
            "reason": reason,
            "message": MESSAGES.get(reason, ""),
            "linked": bool(self.state.get("key")),
            "user": {"username": user.get("username"),
                     "display_name": user.get("display_name"),
                     "iracing_customer_id": user.get("iracing_customer_id")},
            "sim_customer_id": self.sim_cust_id(),
            "offline": bool(self.last_error),
            "grace_days_left": round(grace_left / 86400, 1),
            "last_ok": last_ok,
            "site": SITE,
            "pairing": self.pairing or None,
            "login_pending": bool(self.login),
            "enforced": ENFORCE,
        }
