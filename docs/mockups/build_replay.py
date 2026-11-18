"""Сборка мокапа шторки повтора: шрифты (нужные символы) и логотип SVG — внутрь файла."""
import base64, io, os, re
from fontTools import subset
from fontTools.ttLib import TTFont
APP = r"C:/Users/ssper/irc/app/suite"
t = open("replay-fx.src.html", encoding="utf-8").read()
chars = "".join(sorted(set(t))) + "".join(chr(c) for c in range(0x20, 0x7f)) + "◀●абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
def sub(path):
    f = TTFont(path); opt = subset.Options(); opt.layout_features = ["*"]; opt.notdef_outline = True
    s = subset.Subsetter(opt); s.populate(text=chars); s.subset(f); b = io.BytesIO(); f.save(b)
    return base64.b64encode(b.getvalue()).decode()
svg = open(APP + "/assets/logo-flat.svg", encoding="utf-8").read()
svg = re.sub(r"<\?xml[^>]*>", "", svg).replace("`", "")
t = t.replace("__EXO__", sub(APP + "/web/ui/fonts/Exo2.ttf")).replace("__MONO__", sub(APP + "/web/ui/fonts/RobotoMono.ttf"))
t = t.replace("__LOGO_SVG__", svg)
open("replay-fx.html", "w", encoding="utf-8").write(t)
print(round(os.path.getsize("replay-fx.html") / 1024), "KB")
