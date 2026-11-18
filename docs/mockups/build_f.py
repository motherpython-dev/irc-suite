"""Сборка мокапа стиля F: шрифты (только нужные символы) и логотип — внутрь файла."""
import base64, io, os
from fontTools import subset
from fontTools.ttLib import TTFont
from PIL import Image
APP = r"C:/Users/ssper/irc/app/suite"
t = open("irc-style-f.src.html", encoding="utf-8").read()
chars = "".join(sorted(set(t))) + "".join(chr(c) for c in range(0x20, 0x7f)) + \
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ—«»·…▶−°‹›←→↑↓↖↗↙↘●■▲▼"
def sub(path):
    f = TTFont(path); opt = subset.Options(); opt.layout_features = ["*"]; opt.notdef_outline = True
    s = subset.Subsetter(opt); s.populate(text=chars); s.subset(f); b = io.BytesIO(); f.save(b)
    return base64.b64encode(b.getvalue()).decode()
t = t.replace("__EXO__", sub(APP + "/web/ui/fonts/Exo2.ttf")).replace("__MONO__", sub(APP + "/web/ui/fonts/RobotoMono.ttf"))
im = Image.open(APP + "/assets/logo-irc.png"); im.thumbnail((240, 80)); b = io.BytesIO(); im.save(b, "PNG", optimize=True)
t = t.replace("__LOGO__", base64.b64encode(b.getvalue()).decode())
open("irc-style-f.html", "w", encoding="utf-8").write(t)
print(round(os.path.getsize("irc-style-f.html") / 1024), "KB")
