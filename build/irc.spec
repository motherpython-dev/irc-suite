# PyInstaller spec. Сборка:
#     python -m PyInstaller build/irc.spec --noconfirm --clean --distpath dist --workpath build/_work
#
# --clean обязателен: без него PyInstaller берёт иконку из своего рабочего
# кэша, и новая assets/icon.ico в exe не попадает — так в 2.9.9–2.13.2
# жила старая иконка с кривым кубком, хотя файл давно был исправлен.
#
# В exe попадают: bootstrap (ядро), интерпретатор, зависимости и seed-бандл.
# Логика приложения дальше обновляется бандлами — exe перевыпускается редко.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent

# Панель показывается родным окном на WebView2 (см. suite/core/ui/panel_native.py).
# pywebview тянет за собой .NET-мост pythonnet и сборки WebView2 — их PyInstaller
# сам не находит, поэтому забираем пакеты целиком.
wv_datas, wv_binaries, wv_hidden = [], [], []
for pkg in ("webview", "clr_loader", "pythonnet"):
    d, b, h = collect_all(pkg)
    wv_datas += d
    wv_binaries += b
    wv_hidden += h

a = Analysis(
    [str(ROOT / 'run.py')],
    pathex=[str(ROOT)],
    binaries=wv_binaries,
    datas=[
        (str(ROOT / 'app'), 'seed'),            # вшитый стартовый бандл
        (str(ROOT / 'assets' / 'icon.ico'), 'assets'),
        (str(ROOT / 'assets' / 'logo-irc.png'), 'assets'),  # знак iRC для оверлея
        (str(ROOT / 'assets' / 'fonts'), 'assets/fonts'),  # Inter/Rubik (OFL)
    ] + wv_datas,
    # ВАЖНО: бандл racecontrol загружается ДИНАМИЧЕСКИ из %LOCALAPPDATA% и в
    # анализ не попадает — PyInstaller не видит его импортов. Поэтому всё, что
    # использует бандл, перечисляем здесь руками. Забыл модуль -> exe падает
    # при старте с ModuleNotFoundError (проверено на sqlite3).
    #
    # Сам racecontrol сюда добавлять НЕЛЬЗЯ: frozen-импортёр имеет приоритет
    # над sys.path, и обновлённый бандл никогда бы не загрузился.
    hiddenimports=[
        # зависимости приложения
        'pystray', 'pystray._win32', 'PIL', 'PIL.Image', 'PIL.ImageDraw',
        'irsdk', 'yaml', 'PIL.ImageTk',
        # окна оверлеев поверх сима рисует PIL (suite/core/ui/ov_paint.py)
        'PIL.ImageFont', 'PIL.ImageChops', 'PIL.ImageFilter', 'PIL.ImageColor', 'winreg',
        # stdlib, нужный бандлу
        'sqlite3', 'queue', 'http.server', 'socketserver', 'dataclasses',
        'webbrowser', 'collections.abc', 'typing',
        # оверлей
        'tkinter', 'tkinter.font', 'tkinter.constants',
        # родное окно панели
        'webview', 'webview.platforms.edgechromium', 'clr', 'clr_loader',
        'bottle', 'proxy_tools',
    ] + wv_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'pytest'],   # tkinter нужен оверлею
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='iRC',
    debug=False,
    strip=False,
    upx=False,
    console=False,                              # приложение трея, без консоли
    icon=str(ROOT / 'assets' / 'icon.ico'),
    version_file=None,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name='iRC',
)
