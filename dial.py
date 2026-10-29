"""Dial artwork and icon tinting for the menu bar thermostat popover.

Geometry comes from the Figma design (Scheduled control, node 3:814):
  * dial frame 244 x 244, centre (122, 122)
  * 270 degree sweep, 10 deg at lower left to 30 deg at lower right
  * 81 graduations (one per 0.25 deg), longer at every whole degree
  * target indicator: 2.5pt round-capped stroke, 16pt long, centred on r = 106,
    with the ticks cleared 1.5pt round it so it sits on top
"""
import io
import math
import os
from functools import lru_cache

from PIL import Image, ImageDraw

DIAL = 244
CX = CY = 122.0
MIN_T, MAX_T = 10.0, 30.0
SWEEP = 270.0

R_OUT = 110.0
R_MAJOR = 100.0
R_MINOR = 105.0
TICK_RGB = (74, 76, 83)

IND_R0, IND_R1, IND_W = 98.0, 114.0, 2.5
IND_GAP = 1.5  # clear space between the marker and the ticks
WHITE = (243, 242, 240)
AMBER = (235, 167, 110)

SS = 8      # drawing scale
OUT = 4     # output pixels per point


def assets_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "assets")]
    try:
        from Foundation import NSBundle

        res = NSBundle.mainBundle().resourcePath()
        if res:
            candidates.insert(0, os.path.join(str(res), "assets"))
    except Exception:  # noqa: BLE001
        pass
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[-1]


def angle_for(temp):
    frac = max(0.0, min(1.0, (temp - MIN_T) / (MAX_T - MIN_T)))
    return -SWEEP / 2 + SWEEP * frac


def _pt(r, deg):
    a = math.radians(deg)
    return (CX + r * math.sin(a)) * SS, (CY - r * math.cos(a)) * SS


@lru_cache(maxsize=1)
def _ticks_base():
    """Graduations at output size. Drawn once, then only copied."""
    img = Image.new("RGBA", (DIAL * SS, DIAL * SS), TICK_RGB + (0,))
    d = ImageDraw.Draw(img)
    steps = int(round((MAX_T - MIN_T) * 4))
    for i in range(steps + 1):
        deg = -SWEEP / 2 + SWEEP * i / steps
        r_in = R_MAJOR if i % 4 == 0 else R_MINOR
        d.line([_pt(R_OUT, deg), _pt(r_in, deg)], fill=TICK_RGB + (255,), width=SS)
    return img.reduce(SS // OUT)


@lru_cache(maxsize=256)
def dial_png(target, colour=None, show_indicator=True):
    """PNG bytes (DIAL*OUT square, transparent) for ticks plus target indicator.

    colour is an (r, g, b) tuple for the indicator, or None for white.
    Only a small patch around the indicator is drawn per call, so this is quick.
    """
    img = _ticks_base().copy()
    if show_indicator:
        rgb = tuple(colour or WHITE)
        col = rgb + (255,)
        deg = angle_for(target)
        p0, p1 = _pt(IND_R0, deg), _pt(IND_R1, deg)
        pad = int(IND_W * SS) + 2 * SS
        x0 = int(min(p0[0], p1[0])) - pad
        y0 = int(min(p0[1], p1[1])) - pad
        x1 = int(max(p0[0], p1[0])) + pad
        y1 = int(max(p0[1], p1[1])) + pad
        k = SS // OUT
        x0 -= x0 % k
        y0 -= y0 % k
        w = (x1 - x0) + ((x1 - x0) % k)
        h = (y1 - y0) + ((y1 - y0) % k)
        q0, q1 = (p0[0] - x0, p0[1] - y0), (p1[0] - x0, p1[1] - y0)

        def capsule(fill, width):
            layer = Image.new("RGBA", (w, h), rgb + (0,))
            d = ImageDraw.Draw(layer)
            d.line([q0, q1], fill=fill, width=int(width))
            rr = width / 2
            for x, y in (q0, q1):
                d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=fill)
            return layer.reduce(k)

        # clear the ticks in a thin halo round the marker, so it sits on top of the dial
        halo = capsule(col, (IND_W + 2 * IND_GAP) * SS).getchannel("A")
        ox, oy = x0 // k, y0 // k
        region = img.crop((ox, oy, ox + halo.width, oy + halo.height))
        region.putalpha(Image.composite(Image.new("L", halo.size, 0), region.getchannel("A"), halo))
        img.paste(region, (ox, oy))
        img.alpha_composite(capsule(col, IND_W * SS), (ox, oy))
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


@lru_cache(maxsize=None)
def icon_png(name, rgb, alpha=255):
    """Tint a white Lucide icon master and return PNG bytes."""
    path = os.path.join(assets_dir(), "icons", f"{name}.png")
    master = Image.open(path).convert("RGBA")
    tinted = Image.new("RGBA", master.size, tuple(rgb) + (255,))
    a = master.getchannel("A")
    if alpha != 255:
        a = a.point(lambda v: v * alpha // 255)
    tinted.putalpha(a)
    buf = io.BytesIO()
    tinted.save(buf, "PNG")
    return buf.getvalue()
