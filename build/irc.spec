# PyInstaller spec. Сборка: pyinstaller build/ichamps-racecontrol.spec --noconfirm
#
# В exe попадают: bootstrap (ядро), интерпретатор, зависимости и seed-бандл.
# Логика приложения дальше обновляется бандлами — exe перевыпускается редко.
from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / 'run.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / 'app'), 'seed'),            # вшитый стартовый бандл
        (str(ROOT / 'assets' / 'icon.ico'), 'assets'),
        (str(ROOT / 'assets' / 'icon.png'), 'assets'),   # логотип для оверлея
        (str(ROOT / 'assets' / 'fonts'), 'assets/fonts'),  # Inter/Rubik (OFL)
    ],
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
        # stdlib, нужный бандлу
        'sqlite3', 'queue', 'http.server', 'socketserver', 'dataclasses',
        'webbrowser', 'collections.abc', 'typing',
        # оверлей
        'tkinter', 'tkinter.font', 'tkinter.constants',
    ],
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
