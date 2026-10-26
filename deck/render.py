"""One renderer for both physical keys and the Windows preview."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from .model import Card


SIZE = 72
SCALE = 3


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "segoeuib.ttf" if bold else "segoeui.ttf"
    for path in (Path("C:/Windows/Fonts") / name, Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")):
        if path.exists():
            return ImageFont.truetype(str(path), size * SCALE)
    return ImageFont.load_default(size=size * SCALE)


def _fit(draw: ImageDraw.ImageDraw, value: str, font, width: int) -> str:
    value = " ".join(str(value).split())
    if draw.textbbox((0, 0), value, font=font)[2] <= width:
        return value
    while value and draw.textbbox((0, 0), value + "…", font=font)[2] > width:
        value = value[:-1]
    return value.rstrip() + "…"


def _title_lines(draw: ImageDraw.ImageDraw, value: str, font, width: int) -> list[str]:
    words = value.split()
    if not words:
        return [""]
    lines = [""]
    for word in words:
        candidate = (lines[-1] + " " + word).strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= width:
            lines[-1] = candidate
        elif len(lines) == 1:
            lines.append(word)
        else:
            lines[-1] += " " + word
    return [_fit(draw, line, font, width) for line in lines]


def _rgb(hex_value: str) -> tuple[int, int, int]:
    return tuple(bytes.fromhex(hex_value.lstrip("#")))


def render_key(card: Card, privacy: bool = False) -> Image.Image:
    w = SIZE * SCALE
    im = Image.new("RGB", (w, w), (5, 12, 24))
    draw = ImageDraw.Draw(im)
    accent = _rgb(card.accent)
    for y in range(w):
        fade = y / w
        fill = (int(13 + 4 * (1 - fade)), int(29 + 4 * (1 - fade)), int(48 + 8 * (1 - fade)))
        draw.line((0, y, w, y), fill=fill)
    draw.rounded_rectangle((2*SCALE, 2*SCALE, w-3*SCALE, w-3*SCALE), radius=10*SCALE,
                           outline=tuple(min(255, int(v * .62)) for v in accent), width=SCALE)
    draw.rounded_rectangle((5*SCALE, 5*SCALE, w-6*SCALE, w-6*SCALE), radius=8*SCALE,
                           outline=(48, 70, 94), width=1)
    cx = w // 2
    icon_font = _font(25, True)
    glyph = ""
    if card.kind == "quota":
        draw.arc((17*SCALE, 9*SCALE, 55*SCALE, 47*SCALE), 0, 360, fill=(48, 72, 85), width=4*SCALE)
        try:
            pct = int(card.detail.split("%", 1)[0])
        except ValueError:
            pct = 0
        draw.arc((17*SCALE, 9*SCALE, 55*SCALE, 47*SCALE), -90, -90 + max(0, min(100, pct))*3.6,
                 fill=accent, width=4*SCALE)
        icon_font = _font(15, True)
        glyph = "%"
    elif card.kind == "mail":
        draw.rounded_rectangle((19*SCALE, 17*SCALE, 53*SCALE, 42*SCALE), radius=3*SCALE,
                               outline=accent, width=2*SCALE)
        draw.line((20*SCALE, 19*SCALE, 36*SCALE, 32*SCALE, 52*SCALE, 19*SCALE),
                  fill=accent, width=2*SCALE)
    elif card.kind == "chat":
        draw.rounded_rectangle((19*SCALE, 13*SCALE, 53*SCALE, 39*SCALE), radius=7*SCALE,
                               outline=accent, width=2*SCALE)
        draw.line((27*SCALE, 39*SCALE, 22*SCALE, 47*SCALE, 36*SCALE, 39*SCALE),
                  fill=accent, width=2*SCALE)
    elif card.kind == "project":
        draw.polygon(((15*SCALE, 39*SCALE), (29*SCALE, 17*SCALE), (38*SCALE, 30*SCALE),
                      (46*SCALE, 19*SCALE), (58*SCALE, 39*SCALE)), outline=accent)
        draw.line((15*SCALE, 39*SCALE, 58*SCALE, 39*SCALE), fill=accent, width=2*SCALE)
    elif card.kind in {"codex", "tasks"}:
        glyph = "</>" if card.kind == "tasks" else "C"
    elif card.kind == "calendar":
        draw.rounded_rectangle((20*SCALE, 14*SCALE, 52*SCALE, 44*SCALE), radius=3*SCALE,
                               outline=accent, width=2*SCALE)
        draw.line((20*SCALE, 22*SCALE, 52*SCALE, 22*SCALE), fill=accent, width=2*SCALE)
        for x in (27, 35, 43):
            draw.ellipse((x*SCALE, 29*SCALE, (x+3)*SCALE, 32*SCALE), fill=accent)
    elif card.kind == "talk":
        draw.rounded_rectangle((31*SCALE, 12*SCALE, 41*SCALE, 34*SCALE), radius=5*SCALE,
                               outline=accent, width=2*SCALE)
        draw.arc((24*SCALE, 19*SCALE, 48*SCALE, 43*SCALE), 0, 180, fill=accent, width=2*SCALE)
        draw.line((36*SCALE, 43*SCALE, 36*SCALE, 48*SCALE), fill=accent, width=2*SCALE)
        draw.line((29*SCALE, 48*SCALE, 43*SCALE, 48*SCALE), fill=accent, width=2*SCALE)
    elif card.kind == "device":
        draw.ellipse((23*SCALE, 13*SCALE, 49*SCALE, 39*SCALE), outline=accent, width=3*SCALE)
        draw.ellipse((33*SCALE, 23*SCALE, 39*SCALE, 29*SCALE), fill=accent)
    else:
        glyph = "!"
    if glyph:
        box = draw.textbbox((0, 0), glyph, font=icon_font)
        draw.text((cx-(box[2]-box[0])//2, 12*SCALE), glyph, font=icon_font, fill=accent)
    title_font = _font(9, True)
    detail_font = _font(7)
    title_value = card.title.upper() if not privacy else card.kind.upper()
    title = _fit(draw, title_value, title_font, 61*SCALE)
    detail = _fit(draw, card.detail if not privacy else "Hidden", detail_font, 60*SCALE)
    if title != title_value and card.kind in {"project", "chat", "codex"}:
        lines = _title_lines(draw, title_value, title_font, 61*SCALE)
        for n, line in enumerate(lines[:2]):
            box = draw.textbbox((0, 0), line, font=title_font)
            draw.text((cx-(box[2]-box[0])//2, (45+n*10)*SCALE), line,
                      font=title_font, fill=(244, 249, 255))
    else:
        tbox = draw.textbbox((0, 0), title, font=title_font)
        dbox = draw.textbbox((0, 0), detail, font=detail_font)
        draw.text((cx-(tbox[2]-tbox[0])//2, 47*SCALE), title, font=title_font, fill=(244, 249, 255))
        draw.text((cx-(dbox[2]-dbox[0])//2, 59*SCALE), detail, font=detail_font, fill=(155, 197, 216))
    return im.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def jpeg_key(card: Card, privacy: bool = False) -> bytes:
    image = render_key(card, privacy).rotate(180)
    out = BytesIO()
    image.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def preview(cards: list[Card], privacy: bool = False) -> Image.Image:
    if len(cards) != 15:
        raise ValueError("15 cards required")
    im = Image.new("RGB", (5*92+20, 3*92+20), (8, 15, 27))
    draw = ImageDraw.Draw(im)
    for i, card in enumerate(cards):
        x, y = 18 + (i%5)*92, 18 + (i//5)*92
        draw.rounded_rectangle((x-5, y-5, x+77, y+77), radius=13, fill=(1, 5, 11), outline=(62, 78, 95), width=2)
        im.paste(render_key(card, privacy), (x, y))
    return im
