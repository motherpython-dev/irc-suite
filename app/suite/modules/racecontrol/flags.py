"""Флаги и штрафы — через админ-команды в чат iRacing.

ПОЧЕМУ ТАК. В `irsdk.BroadcastMsg` нет ни одной команды выдачи флагов: перемотку,
камеры и паузу сим принимает по broadcast, а жёлтый флаг и чёрные флаги — только
текстом в чат сессии. Поэтому здесь единственный в проекте кусок, который
эмулирует ввод.

Как это работает:
    1. окно iRacing выводится на передний план (иначе ввод уйдёт не туда);
    2. `chat_command(BeginChat)` открывает строку чата;
    3. текст набирается через SendInput с KEYEVENTF_UNICODE — так раскладка
       клавиатуры не имеет значения (у оператора может быть включена русская);
    4. Enter отправляет.

ТРЕБОВАНИЯ: оператор должен быть админом сессии. Спектейта недостаточно —
команды от не-админа сим молча проигнорирует.

СИНТАКСИС КОМАНД НЕ ПОДТВЕРЖДЁН на живой сессии, поэтому шаблоны вынесены в
конфиг (`chat_commands`): если формат отличается, правится в config.json без
пересборки клиента.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

try:
    import irsdk
except ImportError:
    irsdk = None

user32 = ctypes.windll.user32

# шаблоны админ-команд; {car} — номер машины, {n}/{sec} — числа
DEFAULT_COMMANDS = {
    "yellow": "!y",                      # выбросить full course caution
    "pace_laps": "!pacelaps {n}",        # кругов под жёлтым
    "clear": "!clear {car}",             # снять флаги/штрафы с машины
    "drive_through": "!black {car}",     # drive-through
    "time_penalty": "!black {car} {sec}",  # stop&hold на N секунд
    "dq": "!dq {car}",
    "wave_by": "!waveby {car}",
    "eol": "!eol {car}",
}

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 24)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTunion)]


def _send_inputs(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), ctypes.byref(arr), ctypes.sizeof(INPUT))


def _key_unicode(ch: str) -> list[INPUT]:
    out = []
    for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
        ki = KEYBDINPUT(0, ord(ch), flags, 0, None)
        out.append(INPUT(INPUT_KEYBOARD, _INPUTunion(ki=ki)))
    return out


def _key_vk(vk: int) -> list[INPUT]:
    out = []
    for flags in (0, KEYEVENTF_KEYUP):
        ki = KEYBDINPUT(vk, 0, flags, 0, None)
        out.append(INPUT(INPUT_KEYBOARD, _INPUTunion(ki=ki)))
    return out


def find_sim_window() -> int:
    """HWND окна симулятора. Ищем по заголовку среди видимых окон."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.lower()
            if "iracing.com simulator" in title or title == "iracing simulator":
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


class FlagControl:
    def __init__(self, ir, storage=None, commands: dict | None = None,
                 type_delay: float = 0.004):
        self.ir = ir
        self.storage = storage
        self.commands = dict(DEFAULT_COMMANDS)
        if commands:
            self.commands.update({k: v for k, v in commands.items() if v})
        self.type_delay = type_delay
        self.last_error = ""

    # ---------- отправка ----------

    def send_chat(self, text: str) -> bool:
        """Набрать команду в чате сима. Возвращает False, если не вышло."""
        if not self.ir or irsdk is None:
            self.last_error = "SDK недоступен"
            return False
        hwnd = find_sim_window()
        if not hwnd:
            self.last_error = "Окно iRacing не найдено"
            return False

        prev = user32.GetForegroundWindow()
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.12)                      # сим должен успеть принять фокус

        try:
            self.ir.chat_command(irsdk.ChatCommandMode.begin_chat)
            time.sleep(0.18)                  # строка чата открывается не мгновенно
            for ch in text:
                _send_inputs(_key_unicode(ch))
                time.sleep(self.type_delay)
            _send_inputs(_key_vk(VK_RETURN))
        except Exception as e:                # noqa: BLE001 - показываем оператору
            self.last_error = str(e)
            return False
        finally:
            # фокус возвращаем только если он был не у сима
            if prev and prev != hwnd:
                try:
                    user32.SetForegroundWindow(prev)
                except Exception:
                    pass

        self._log("chat_command_sent", text=text)
        return True

    def _log(self, kind: str, **payload) -> None:
        if self.storage:
            try:
                self.storage.log_event(kind, payload)
            except Exception:
                pass

    def _run(self, key: str, **fmt) -> bool:
        tpl = self.commands.get(key)
        if not tpl:
            self.last_error = f"Команда {key} не настроена"
            return False
        return self.send_chat(tpl.format(**fmt))

    # ---------- флаги сессии ----------

    def yellow(self) -> bool:
        return self._run("yellow")

    def pace_laps(self, laps: int) -> bool:
        return self._run("pace_laps", n=max(0, int(laps)))

    # ---------- по конкретной машине ----------

    def clear(self, car: str) -> bool:
        """Снять чёрные флаги и штрафы с машины."""
        return self._run("clear", car=str(car))

    def drive_through(self, car: str) -> bool:
        return self._run("drive_through", car=str(car))

    def time_penalty(self, car: str, seconds: int) -> bool:
        return self._run("time_penalty", car=str(car), sec=int(seconds))

    def disqualify(self, car: str) -> bool:
        return self._run("dq", car=str(car))

    def wave_by(self, car: str) -> bool:
        return self._run("wave_by", car=str(car))
