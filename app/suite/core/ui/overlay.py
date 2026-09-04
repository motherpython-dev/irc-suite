"""Базовое окно оверлея — общее для всех модулей.

Три свойства, без которых окно поверх симулятора бесполезно:

* `-topmost` — рисуется поверх iRacing. Требует, чтобы сим был в режиме
  borderless windowed: в exclusive fullscreen поверх окна не рисуется ничего.
* `WS_EX_NOACTIVATE` — клик по оверлею не забирает фокус у сима. Иначе каждое
  касание отбирало бы у гонки управление.
* `-transparentcolor` — вне панели мышь проходит насквозь, в сим.

Плюс `WS_EX_TOOLWINDOW`, чтобы окна не забивали Alt+Tab: их бывает пять штук.

Окно перетаскивается за любую точку шапки, позиция запоминается в конфиге
модуля — режиссёр раскладывает графику по экрану один раз.

Появление и уход анимированы: графика, возникающая мгновенно, в эфире
выглядит как сбой картинки. Окно выезжает со сдвигом и проявляется, уходит
тем же путём назад. Направление задаёт наследник через ENTER.
"""
from __future__ import annotations

import time
import tkinter as tk

from ...modules.racecontrol.overlay import (  # общий UI-kit живёт там
    ACCENT, BG, DIM, FG, LIGHT, LINE, LIVE_C, SURFACE, SURFACE_2, SURFACE_3,
    TRANSPARENT, RADIUS_CARD, _apply_no_activate, apply_round_corners,
    center_text, pick_font, register_bundled_fonts, rounded_png,
)


class OverlayWindow:
    """Окно поверх сима: рамка, перетаскивание, показ и скрытие.

    Наследник описывает содержимое в `build()` и обновляет его в `refresh()`.
    """

    WIDTH = 360
    TITLE = "iRC"
    CONFIG_PREFIX = "ov"

    # Откуда выезжает окно: "left", "right", "up", "down" или "fade".
    ENTER = "left"
    ANIM_MS = 220               # длительность появления
    ANIM_SHIFT = 26             # на сколько пикселей сдвигается при выезде

    def __init__(self, module, host_root=None, config_key: str | None = None):
        self.module = module
        self.cfg = module.ctx.config
        self.key = config_key or self.CONFIG_PREFIX
        self.visible = False
        self._drag = (0, 0)
        self._keep: list = []
        self._anim = None            # id запланированного шага анимации
        self._home = None            # куда окно возвращается после выезда

        register_bundled_fonts()
        self.f_display = pick_font("Rubik", "Inter")
        self.f_ui = pick_font("Inter", "Rubik")

        parent = host_root
        self.root = tk.Toplevel(parent) if parent is not None else tk.Tk()
        self.root.withdraw()
        self.root.title(self.TITLE)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", float(self.cfg.get("overlay_alpha", 0.96)))
        self.root.configure(bg=TRANSPARENT)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            pass                      # не Windows — окно будет обычным

        pos = self.cfg.setdefault("positions", {}).get(self.key, {})
        x = int(pos.get("x", 40))
        y = int(pos.get("y", 120))
        self.root.geometry(f"{self.WIDTH}x200+{x}+{y}")

        outer = tk.Frame(self.root, bg=TRANSPARENT)
        outer.pack(fill="both", expand=True)
        self.panel = tk.Frame(outer, bg=BG, highlightthickness=1,
                              highlightbackground=LINE)
        self.panel.pack(fill="both", expand=True)

        self.build()
        self.root.update_idletasks()
        self.fit()
        _apply_no_activate(self.root)
        apply_round_corners(self.root, RADIUS_CARD)
        self._tick()

    # ---------- переопределяется наследником ----------

    def build(self) -> None:
        raise NotImplementedError

    def refresh(self) -> None:
        raise NotImplementedError

    # ---------- перетаскивание ----------

    def make_draggable(self, *widgets) -> None:
        for w in widgets:
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            try:
                w.configure(cursor="fleur")
            except tk.TclError:
                pass

    def _drag_start(self, e) -> None:
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e) -> None:
        x, y = e.x_root - self._drag[0], e.y_root - self._drag[1]
        self.root.geometry(f"+{x}+{y}")
        self._home = (x, y)
        self.cfg.setdefault("positions", {})[self.key] = {"x": x, "y": y}

    # ---------- показ ----------

    def show(self) -> None:
        if self.visible:
            return
        self.visible = True
        self._stop_anim()
        self._remember_home()
        self.root.attributes("-alpha", 0.0)
        self._place(0.0)
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        _apply_no_activate(self.root)
        apply_round_corners(self.root, RADIUS_CARD)
        self._run_anim(0.0, 1.0)

    def hide(self) -> None:
        if not self.visible:
            self.root.withdraw()
            return
        self.visible = False
        self._stop_anim()
        self._remember_home()
        self._run_anim(1.0, 0.0, done=self.root.withdraw)

    # ---------- анимация ----------

    def _remember_home(self) -> None:
        """Запомнить точку, из которой окно выезжает и куда возвращается."""
        if self._home is None:
            pos = self.cfg.get("positions", {}).get(self.key, {})
            try:
                x = int(pos.get("x", self.root.winfo_x()))
                y = int(pos.get("y", self.root.winfo_y()))
            except Exception:
                x, y = 40, 120
            self._home = (x, y)

    def _stop_anim(self) -> None:
        if self._anim is not None:
            try:
                self.root.after_cancel(self._anim)
            except Exception:
                pass
            self._anim = None

    def _place(self, p: float) -> None:
        """Поставить окно в точку по ходу анимации: p=0 — вне, p=1 — на месте."""
        if not self._home:
            return
        x, y = self._home
        shift = int(self.ANIM_SHIFT * (1.0 - p))
        if self.ENTER == "left":
            x -= shift
        elif self.ENTER == "right":
            x += shift
        elif self.ENTER == "up":
            y += shift
        elif self.ENTER == "down":
            y -= shift
        self.root.geometry(f"+{x}+{y}")
        base = float(self.cfg.get("overlay_alpha", 0.96))
        self.root.attributes("-alpha", max(0.0, min(1.0, base * p)))

    def _run_anim(self, start: float, end: float, done=None) -> None:
        """Прогнать анимацию между двумя состояниями.

        Кадры считаем от часов, а не от номера шага: при загруженном Tk шаги
        приходят неравномерно, и счёт по номеру давал бы рывки.
        """
        if not bool(self.cfg.get("animate", True)):
            self._place(end)
            if done:
                done()
            return

        started = time.monotonic()
        dur = max(0.05, self.ANIM_MS / 1000.0)

        def step():
            t = min(1.0, (time.monotonic() - started) / dur)
            # ease-out: быстро стартует, мягко останавливается
            eased = 1.0 - (1.0 - t) ** 3
            self._place(start + (end - start) * eased)
            if t < 1.0:
                self._anim = self.root.after(16, step)
                return
            self._anim = None
            if done:
                done()

        step()

    def set_visible(self, visible: bool) -> None:
        self.show() if visible else self.hide()

    def stop(self) -> None:
        try:
            self.module.ctx.save_config()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------- служебное ----------

    def fit(self) -> None:
        """Подогнать высоту под содержимое: Tk масштабирует шрифты по-своему,
        и фиксированная высота на другой машине обрежет низ."""
        try:
            self.root.update_idletasks()
            h = max(60, self.panel.winfo_reqheight())
            self.root.geometry(f"{self.WIDTH}x{h}")
        except Exception:
            pass

    def head(self, title: str) -> tk.Frame:
        """Шапка в фирменном стиле; за неё же окно и таскают."""
        bar = tk.Frame(self.panel, bg=ACCENT)
        bar.pack(fill="x")
        lbl = tk.Label(bar, text=title.upper(), bg=ACCENT, fg="#FFFFFF",
                       font=(self.f_display, 8, "bold"), anchor="w")
        lbl.pack(side="left", padx=10, pady=5)
        self.meta = tk.Label(bar, text="", bg=ACCENT, fg="#FFE8DC",
                             font=(self.f_ui, 8))
        self.meta.pack(side="right", padx=10)
        self.make_draggable(bar, lbl, self.meta)
        return bar

    def _tick(self) -> None:
        if self.visible:
            try:
                self.refresh()
            except Exception:
                pass
        self.root.after(int(1000 / max(1, self.cfg.get("refresh_hz", 3))), self._tick)
