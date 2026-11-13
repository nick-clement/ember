#!/usr/bin/env python3
"""Hive heating control in the macOS menu bar.

Uses the unofficial pyhiveapi library (pip package: pyhive-integration).
Login details and the device token are kept in the macOS Keychain.

UI follows the Figma designs: a compact graphite popover with a fine dial,
a scheduled / boost control pane, and a settings and mode pane.
"""
import copy
import json
import math
import os
import re
import threading
import time
import traceback
from datetime import datetime, timedelta

import AppKit as AK
import keyring
import objc
import requests
import rumps
from Foundation import NSData, NSObject, NSUserDefaults
from PyObjCTools import AppHelper
from pyhiveapi import Hive, SMS_REQUIRED
from pyhiveapi.api.hive_api import HiveApi

import dial

# pyhiveapi's synchronous build uses three threading names that don't exist
# (leftovers from its async version). Give it working stand-ins. Without TimeoutError,
# any failed refresh shows "module 'threading' has no attribute 'TimeoutError'".
threading.current_task = threading.current_thread
threading.sleep = time.sleep
threading.TimeoutError = TimeoutError

# pyhiveapi reads Hive's sign-in settings from sso.hivehome.com on every sign-in, with
# certificate checks turned off. That page started returning Access Denied on 2026-10-29.
# The settings are fixed values, so use them directly: sign-in no longer depends on that
# page, and nothing is fetched without checking the certificate.
HIVE_LOGIN_INFO = {
    "UPID": "eu-west-1_SamNfoWtf",              # Hive's Cognito user pool
    "CLIID": "3rl4i0ajrmtdm8sbre54p9dvd9",      # Hive's public app client
    "REGION": "eu-west-1_SamNfoWtf",            # the library takes the region from the pool id
}
HiveApi.getLoginInfo = lambda self: dict(HIVE_LOGIN_INFO)

SERVICE = "HiveMenuBar"
LOG_PATH = os.path.expanduser("~/Library/Logs/Ember.log")
STEP = 1.0
MIN_TEMP, MAX_TEMP = 5.0, 32.0
BOOST_MINUTES = 60
BOOST_OFFSET = 2.0          # boost target = current target + 2 degrees
REFRESH_SECONDS = 60
HISTORY_HOURS = 24
HISTORY_SECONDS = 15 * 60   # Hive keeps a reading every 30 minutes, so this is plenty
HISTORY_URL = "https://beekeeper-uk.hivehome.com/1.0/history/heating/{0}"
WEATHER_URL = "https://weather.prod.bgchprod.info/weather"  # Hive's own, by postcode; the Hive app uses it
WEATHER_SECONDS = 15 * 60

# ---------------------------------------------------------------- design tokens
POPOVER_W = 360
GROW = POPOVER_W - 320      # layouts were drawn at 320pt; right-hand parts move over by this
CONTENT_W = POPOVER_W - 32  # 16pt margins
ARROW_H = 8
APP_NAME = "Ember"
VERSION = "1.7"  # setup.py reads this for the app bundle; release.sh bumps it
UPDATE_URL = os.environ.get("EMBER_UPDATE_URL",  # set only to test against a local copy
                            "https://api.github.com/repos/nick-clement/ember/releases/latest")  # made by release.sh
UPDATE_SECONDS = 6 * 3600  # how often to look for a newer release
UPDATE_DIR = os.path.expanduser("~/Library/Caches/Ember/update")
BODY_H = {"control": 572, "settings": 521, "signin": 379, "code": 248, "schedule": 300, "zones": 160}
SIGNIN_HINT = "Your home, from your menu bar."
FORGOT_URL = "https://my.hivehome.com/"

# Matte near-black, flat surfaces, one accent (amber). Braun / Teenage Engineering feel.
BG = "#121212"
BORDER = "#2a2a2a"
SURFACE = "#1f1f1f"
HOVER = "#2a2a2a"       # a SURFACE control under the pointer
ROW_HOVER = "#1b1b1b"
SEPARATOR = "#242424"
TEXT = "#f2f2f0"
TEXT_2 = "#ababab"
TEXT_3 = "#949494"   # 4.5:1 or better on every surface, HOVER included
AMBER = "#eba76e"
AMBER_SURFACE = "#2e2219"
AMBER_HOVER = "#3a2a1e"
ERROR = "#e0877b"
FIELD_BG = "#0c0c0c"

# While heating, the control pane turns orange, after the Barbican signs: black text, white numeral.
ORANGE = "#f25a0c"
ORANGE_RED = "#e8461e"   # high targets shade towards this, as amber shades to red
ON_ORANGE = "#111111"
ON_ORANGE_2 = "#2a0e02"  # 4.5:1 or better on ORANGE, ORANGE_RED and their hover fills

# Type: every label is SF Mono in capitals; only the big target numeral is SF Pro Heavy.
# VoiceOver is given the normal words (see set_label), not the capitals.
CAPS = True
MONO_TRACKING = 0.05     # extra letter spacing for SF Mono, as a share of the font size
NUMERAL_PT = 127        # with -8% letter spacing, as in Figma
OFF_PT = 103             # "Off" in the numeral's place, a little smaller than a number, -5% spacing
DEGREE_RATIO = 36 / 127  # the ° is 36pt beside a full-size number
NUMERAL_W = 192          # the numeral and its ° fit in this; longer values get smaller
RED = "#e8604e"

# Heat colour: amber up to WARM_TEMP, shading to red by HOT_TEMP.
WARM_TEMP = 23.0
HOT_TEMP = 26.0
WARN_TEMP = 24.0       # show a warning line from here
VERY_HIGH_TEMP = 28.0  # stronger wording from here
CONFIRM_TEMP = 26.0    # typed-in values above this ask first
BOOST_CAP = 25.0       # a boost never aims above this unless the target already does
HISTORY_HOURS_Y = 538  # hour marks under the Last 24 hours chart
NOTE_Y = 348           # the note under Boost; a second line moves it up to 341, clear of Last 24 hours
SEND_DELAY = 0.8       # seconds of quiet after the last +/- tap before we call Hive


def heat_hex(temp):
    """Amber for normal heating, blending to red as the target climbs."""
    f = max(0.0, min(1.0, (temp - WARM_TEMP) / (HOT_TEMP - WARM_TEMP)))
    a = [int(AMBER[i:i + 2], 16) for i in (1, 3, 5)]
    r = [int(RED[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(a[i] + (r[i] - a[i]) * f) for i in range(3))

# Placeholder shown before sign-in completes.
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def next_heat_on(schedule, room, now=None, on=True):
    """When the schedule next asks for more than the room temperature, as a timestamp.

    schedule is Hive's raw weekly schedule: {"monday": [{"start": minutes, "value": {"target": t}}], ...}.
    With on=False, when it next asks for no more than the room, so the heating goes off.
    Returns None if nothing in the coming week would do it.
    """
    now = now or datetime.now()
    # the slot already running counts: if it asks for more than the room has, heat is due now.
    # (Only for on=True: while heating, Hive runs a little past the target, so "off" means a later slot.)
    current_target = schedule_target_at(schedule, now.timestamp())
    if on and current_target is not None and float(current_target) > room:
        return now.timestamp()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for day in range(8):
        date = midnight + timedelta(days=day)
        slots = sorted(schedule.get(_DAYS[date.weekday()]) or [], key=lambda s: s.get("start", 0))
        for slot in slots:
            start = date + timedelta(minutes=slot.get("start", 0))
            value = slot.get("value") or {}
            target = value.get("target", value.get("heat"))
            if start > now and target is not None and (float(target) > room) == on:
                return start.timestamp()
    return None


def when_text(ts, now=None, clock_only=False):
    """'at 16:30', 'tomorrow at 06:30' or 'Mon at 06:30'. clock_only drops the 'at'."""
    now = now or datetime.now()
    at = datetime.fromtimestamp(ts)
    days = (at.date() - now.date()).days
    clock = ("" if clock_only else "at ") + at.strftime("%H:%M")
    if days == 0:
        return clock
    if days == 1:
        return f"tomorrow {clock}"
    return f"{at.strftime('%a')} {clock}"


def schedule_target_at(schedule, ts):
    """The target Hive's weekly schedule asks for at a timestamp, or None."""
    at = datetime.fromtimestamp(ts)
    minute = at.hour * 60 + at.minute
    for back in range(8):
        day = at - timedelta(days=back)
        slots = sorted(schedule.get(_DAYS[day.weekday()]) or [], key=lambda s: s.get("start", 0))
        if back == 0:
            slots = [s for s in slots if s.get("start", 0) <= minute]
        if slots:
            value = slots[-1].get("value") or {}
            target = value.get("target", value.get("heat"))
            return None if target is None else float(target)
    return None


def parse_history(payload):
    """Hive's history reply as [(timestamp, temperature)], oldest first."""
    points = []
    for row in (payload or {}).get("data") or []:
        try:
            points.append((int(row["date"]) / 1000.0, float(row["temperature"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(points)


def day_slots(schedule, day):
    """[(start minute, target)] for one weekday (0 = Monday), in time order."""
    out = []
    for slot in (schedule or {}).get(_DAYS[day % 7]) or []:
        try:
            out.append((int(slot["start"]), float(slot["value"]["target"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def temp_text(t):
    return f"{int(t)}°" if float(t).is_integer() else f"{t:.1f}°"


# Rough sunrise and sunset in the UK by month, local clock time. Only picks a sun or moon icon.
SUN_HOURS = ((8.0, 16.3), (7.4, 17.2), (6.4, 18.0), (6.4, 19.9), (5.3, 20.7), (4.8, 21.3),
             (5.1, 21.1), (5.9, 20.3), (6.7, 19.2), (7.4, 18.0), (7.2, 16.4), (8.0, 15.9))


def is_night(now=None):
    now = now or datetime.now()
    rise, fall = SUN_HOURS[now.month - 1]
    hour = now.hour + now.minute / 60.0
    return hour < rise or hour >= fall


def weather_icon(description, night=False):
    """A Lucide icon for Hive's weather description ('few clouds', 'light rain', ...)."""
    d = (description or "").lower()
    for words, icon in ((("thunder", "storm"), "cloud-lightning"),
                        (("snow", "sleet", "hail"), "cloud-snow"),
                        (("drizzle",), "cloud-drizzle"),
                        (("rain", "shower"), "cloud-rain"),
                        (("fog", "mist", "haze", "smoke"), "cloud-fog")):
        if any(w in d for w in words):
            return icon
    if any(w in d for w in ("few", "scattered", "partly")):
        return "cloud-moon" if night else "cloud-sun"
    if "cloud" in d or "overcast" in d:
        return "cloud"
    return "moon" if night else "sun"


def parse_weather(payload):
    """(temperature in °C, description) from Hive's weather reply."""
    w = payload["weather"]
    return float(w["temperature"]["value"]), str(w.get("description") or "")


def zone_names(session, devices):
    """Each heating zone's name as the Hive app shows it ("Downstairs", "Upstairs")."""
    names = []
    for i, device in enumerate(devices):
        try:
            name = session.data.products[device["hiveID"]]["state"]["name"]
        except Exception:  # noqa: BLE001
            name = None
        name = name or device.get("hiveName") or "Heating"
        if name in names:
            name = f"{name} {i + 1}"
        names.append(name)
    return names


DUMMY_STATE = {
    "current": 19.1, "target": 19.0, "mode": "SCHEDULE",
    "boost": False, "heating": False, "boost_end": None,
}


def rgb(hex_, alpha=1.0):
    h = hex_.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return AK.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)


def rgb_tuple(hex_):
    h = hex_.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def press_hex(hex_, amount=0.08):
    """One step on from a hover fill: lighter on dark surfaces, darker on light ones."""
    c = rgb_tuple(hex_)
    to = 0 if sum(c) / 3 > 128 else 255
    return "#%02x%02x%02x" % tuple(round(v + (to - v) * amount) for v in c)


def log_error(where):
    """Append the current exception, with its traceback, to LOG_PATH."""
    try:
        with open(LOG_PATH, "a") as f:
            text = re.sub(r"eyJ[\w.-]{20,}", "[token]", traceback.format_exc())  # keep sign-in tokens out of the log
            f.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} {where}\n{text}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- keychain
def kc_get(key):
    return keyring.get_password(SERVICE, key)


def kc_set(key, value):
    keyring.set_password(SERVICE, key, value)


# ---------------------------------------------------------------- small AppKit helpers
_FONT_WEIGHT = {
    "light": AK.NSFontWeightLight,
    "regular": AK.NSFontWeightRegular,
    "medium": AK.NSFontWeightMedium,
    "semibold": AK.NSFontWeightSemibold,
}
_ALIGN = {
    "left": AK.NSTextAlignmentLeft,
    "center": AK.NSTextAlignmentCenter,
    "right": AK.NSTextAlignmentRight,
}


def font(size, weight="regular"):
    """SF Mono for all text. "heavy-display" is SF Pro Heavy, used for the big numeral only.

    Older weight names ("mono-regular", "code-medium") still work and are mono too.
    """
    if weight == "heavy-display":
        return AK.NSFont.systemFontOfSize_weight_(size, AK.NSFontWeightHeavy)
    weight = weight.split("-")[-1]
    return AK.NSFont.monospacedSystemFontOfSize_weight_(size, _FONT_WEIGHT[weight])


def is_sentence(text):
    """Full sentences (ending in a full stop) stay in sentence case: long text is slow to read in capitals."""
    return text.rstrip().endswith(".") and " " in text.strip()


def attributed(parts, align="left", wrap=False):
    """parts: list of (text, size, weight, hex colour[, kern])."""
    para = AK.NSMutableParagraphStyle.alloc().init()
    para.setAlignment_(_ALIGN[align])
    para.setLineBreakMode_(AK.NSLineBreakByWordWrapping if wrap else AK.NSLineBreakByClipping)
    out = AK.NSMutableAttributedString.alloc().init()
    for part in parts:
        text, size, weight, colour = part[:4]
        kern = part[4] if len(part) > 4 else 0.0
        if CAPS and not is_sentence(text):
            text = text.upper()
        if len(part) <= 4 and weight != "heavy-display":
            kern = MONO_TRACKING * size  # SF Mono, opened up a touch
        attrs = {
            AK.NSFontAttributeName: font(size, weight),
            AK.NSForegroundColorAttributeName: rgb(colour),
            AK.NSParagraphStyleAttributeName: para,
            AK.NSKernAttributeName: kern,
        }
        out.appendAttributedString_(
            AK.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        )
    return out


class Label(AK.NSTextField):
    """A text label that tells VoiceOver its words as written, though they show in capitals."""

    plain = None

    def accessibilityValue(self):
        return self.plain if self.plain is not None else self.stringValue()


def make_label(parts, x, y, w, h, align="left", wrap=False):
    tf = Label.alloc().initWithFrame_(AK.NSMakeRect(x, y, w, h))
    tf.setBezeled_(False)
    tf.setBordered_(False)
    tf.setDrawsBackground_(False)
    tf.setEditable_(False)
    tf.setSelectable_(False)
    if wrap:
        try:
            tf.cell().setWraps_(True)
        except Exception:  # noqa: BLE001
            pass
    set_label(tf, parts, align, wrap)
    return tf


def set_label(tf, parts, align="left", wrap=False):
    tf.setAttributedStringValue_(attributed(parts, align, wrap))
    if isinstance(tf, Label):  # shown in capitals; VoiceOver gets the words as written
        tf.plain = "".join(p[0] for p in parts)


def set_plain(tf, text):
    """What VoiceOver reads for a label, when it needs more than the words shown."""
    if isinstance(tf, Label):
        tf.plain = text


def reduce_motion():
    try:
        return bool(AK.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:  # noqa: BLE001
        return False


def plain_text(tf):
    """A label's words as written (before CAPS), for VoiceOver."""
    return tf.accessibilityValue() if isinstance(tf, Label) else tf.stringValue()


def text_width(parts):
    return attributed(parts).size().width


def ns_image(png_bytes, pt):
    data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    img = AK.NSImage.alloc().initWithData_(data)
    img.setSize_((pt, pt))
    return img


def icon_view(name, colour, pt, x, y, alpha=255):
    img = ns_image(dial.icon_png(name, rgb_tuple(colour), alpha), pt)
    view = AK.NSImageView.alloc().initWithFrame_(AK.NSMakeRect(x, y, pt, pt))
    view.setImage_(img)
    view.setImageScaling_(AK.NSImageScaleProportionallyUpOrDown)
    return view


def set_icon(view, name, colour, pt):
    view.setImage_(ns_image(dial.icon_png(name, rgb_tuple(colour)), pt))


# ---------------------------------------------------------------- views
class FlippedView(AK.NSView):
    """Top-left origin, so layout numbers can be copied straight from Figma."""

    def isFlipped(self):
        return True


class Box(FlippedView):
    """A plain filled rectangle (separators)."""

    def initWithFrame_(self, frame):
        self = objc.super(Box, self).initWithFrame_(frame)
        self.colour = None
        return self

    def drawRect_(self, rect):
        if self.colour is not None:
            self.colour.setFill()
            AK.NSRectFill(self.bounds())


class Hit(FlippedView):
    """A clickable rounded surface with hover and pressed states.

    VoiceOver sees it as a button, named by ax_label or else by the text inside it.
    Tab reaches it (see PopoverUI.focus_next); Space or Return presses it.
    """

    tabbing = False  # set while Tab moves focus, so a mouse click never takes focus

    def initWithFrame_(self, frame):
        self = objc.super(Hit, self).initWithFrame_(frame)
        if self is None:
            return None
        self.callback = None
        self.radius = 0.0
        self.fill = None
        # Tab focus: our own 2pt ring in the pane's text colour. The system's blue ring
        # is under 3:1 on orange (1.1:1) and on near-black (2.7:1).
        self.focus_hex = TEXT
        self.setFocusRingType_(AK.NSFocusRingTypeNone)
        self.hover_fill = None
        self.pressed_fill = None
        self.border = None
        self.border_w = 1.0
        self.hovering = False
        self.focused = False
        self.on_active = None  # called with True/False as hover or Tab focus starts and ends
        self.pressed = False
        self.enabled = True
        self.ax_label = None
        self.ax_selected = False
        area = AK.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            AK.NSTrackingMouseEnteredAndExited
            | AK.NSTrackingActiveAlways
            | AK.NSTrackingInVisibleRect,
            self,
            None,
        )
        self.addTrackingArea_(area)
        return self

    @objc.python_method
    def configure(self, radius, fill=None, hover=None, border=None, border_w=1.0, pressed=None):
        """pressed defaults to one step on from hover (see press_hex)."""
        self.radius = radius
        self.fill = rgb(fill) if fill else None
        self.hover_fill = rgb(hover) if hover else None
        self.pressed_fill = rgb(pressed or press_hex(hover)) if hover else None
        self.border = rgb(border) if border else None
        self.border_w = border_w
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        fill = self.fill
        if self.enabled and self.hovering and self.hover_fill is not None:
            fill = self.hover_fill
        if self.enabled and self.pressed and self.pressed_fill is not None:
            fill = self.pressed_fill
        if fill is not None or self.border is not None:
            inset = self.border_w / 2.0 if self.border is not None else 0.0
            r = AK.NSInsetRect(self.bounds(), inset, inset)
            path = AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                r, self.radius, self.radius
            )
            if fill is not None:
                fill.setFill()
                path.fill()
            if self.border is not None:
                self.border.setStroke()
                path.setLineWidth_(self.border_w)
                path.stroke()
        window = self.window()
        if window is not None and window.firstResponder() is self:
            self.draw_focus()  # on top of the fill

    @objc.python_method
    def draw_focus(self):
        ring = AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AK.NSInsetRect(self.bounds(), 1, 1), max(0.0, self.radius - 1), max(0.0, self.radius - 1))
        ring.setLineWidth_(2.0)
        rgb(self.focus_hex).setStroke()
        ring.stroke()

    def becomeFirstResponder(self):
        self.focused = True
        self.setNeedsDisplay_(True)
        self._active_changed()
        return True

    def resignFirstResponder(self):
        self.focused = False
        self.setNeedsDisplay_(True)
        self._active_changed()
        return True

    @objc.python_method
    def _active_changed(self):
        if self.on_active is not None:
            self.on_active(self.hovering or self.focused)

    def hitTest_(self, point):
        local = self.convertPoint_fromView_(point, self.superview())
        return self if AK.NSPointInRect(local, self.bounds()) else None

    # ---- VoiceOver
    def isAccessibilityElement(self):
        return self.callback is not None

    def accessibilityRole(self):
        return AK.NSAccessibilityButtonRole

    def accessibilityLabel(self):
        label = self.ax_label
        if not label:
            texts = [plain_text(v) for v in self.subviews()
                     if isinstance(v, AK.NSTextField) and not v.isHidden() and v.stringValue()]
            label = ", ".join(texts)
        return f"{label}, selected" if self.ax_selected else label

    def accessibilityChildren(self):
        return []

    def isAccessibilityEnabled(self):
        return self.enabled

    def accessibilityPerformPress(self):
        if self.enabled and self.callback is not None:
            self.callback()
            return True
        return False

    # ---- keyboard
    def acceptsFirstResponder(self):
        return Hit.tabbing and self.enabled and self.callback is not None

    def keyDown_(self, event):
        if event.charactersIgnoringModifiers() in (" ", "\r", "\x03") and self.enabled:
            if self.callback is not None:
                self.callback()
            return
        objc.super(Hit, self).keyDown_(event)

    def acceptsFirstMouse_(self, event):
        return True

    def mouseEntered_(self, event):
        self.hovering = True
        self.setNeedsDisplay_(True)
        self._active_changed()

    def mouseExited_(self, event):
        self.hovering = False
        self.pressed = False
        self.setNeedsDisplay_(True)
        self._active_changed()

    def mouseDown_(self, event):
        window = self.window()
        if window is not None and isinstance(window.firstResponder(), Hit):
            window.makeFirstResponder_(window)  # a click clears the Tab focus ring
        if self.enabled:
            self.pressed = True
            self.setNeedsDisplay_(True)

    def mouseUp_(self, event):
        was_pressed = self.pressed
        self.pressed = False
        self.setNeedsDisplay_(True)
        local = self.convertPoint_fromView_(event.locationInWindow(), None)
        if was_pressed and self.enabled and AK.NSPointInRect(local, self.bounds()):
            if self.callback is not None:
                self.callback()


class PopoverBackground(FlippedView):
    """Rounded graphite body with the small anchor arrow towards the menu bar."""

    def initWithFrame_(self, frame):
        self = objc.super(PopoverBackground, self).initWithFrame_(frame)
        self.arrow_x = 237.0
        self.glass = False  # sign-in panes: translucent tint over a blurred backdrop
        self.fill_hex, self.stroke_hex = BG, BORDER  # orange on the control pane while heating
        return self

    def drawRect_(self, rect):
        b = self.bounds()
        w, h = b.size.width, b.size.height
        top = ARROW_H + 0.5
        left, right, bottom, r = 0.5, w - 0.5, h - 0.5, 14.0
        ax = self.arrow_x
        path = AK.NSBezierPath.bezierPath()
        path.moveToPoint_((left + r, top))
        path.lineToPoint_((ax - 8, top))
        path.lineToPoint_((ax, 0.5))
        path.lineToPoint_((ax + 8, top))
        path.lineToPoint_((right - r, top))
        path.appendBezierPathWithArcFromPoint_toPoint_radius_((right, top), (right, top + r), r)
        path.lineToPoint_((right, bottom - r))
        path.appendBezierPathWithArcFromPoint_toPoint_radius_((right, bottom), (right - r, bottom), r)
        path.lineToPoint_((left + r, bottom))
        path.appendBezierPathWithArcFromPoint_toPoint_radius_((left, bottom), (left, bottom - r), r)
        path.lineToPoint_((left, top + r))
        path.appendBezierPathWithArcFromPoint_toPoint_radius_((left, top), (left + r, top), r)
        path.closePath()
        if self.glass:
            rgb("#262a30", 0.79).setFill()
            path.fill()
            rgb("#ffffff", 0.15).setStroke()
        else:
            rgb(self.fill_hex).setFill()
            path.fill()
            rgb(self.stroke_hex).setStroke()
        path.setLineWidth_(1.0)
        path.stroke()


class FieldBox(FlippedView):
    """Rounded, slightly inset surface that holds a text field."""

    def initWithFrame_(self, frame):
        self = objc.super(FieldBox, self).initWithFrame_(frame)
        self.field = None
        self.glass = False
        return self

    def drawRect_(self, rect):
        r = AK.NSInsetRect(self.bounds(), 0.5, 0.5)
        radius = 7.0 if self.glass else 8.0
        path = AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, radius, radius)
        if self.glass:
            rgb("#181b20", 0.5).setFill()
            path.fill()
            rgb("#ffffff", 0.11).setStroke()
        else:
            rgb(FIELD_BG).setFill()
            path.fill()
            rgb(BORDER).setStroke()
        path.setLineWidth_(1.0)
        path.stroke()

    def mouseDown_(self, event):
        if self.field is not None:
            self.window().makeFirstResponder_(self.field)


class DayBar(FlippedView):
    """One day of the schedule as a 24-hour strip, warmer periods in stronger amber."""

    def initWithFrame_(self, frame):
        self = objc.super(DayBar, self).initWithFrame_(frame)
        self.spans = []    # [(start minute, end minute, target)]
        self.now = None    # minute of the day to mark, or None
        return self

    @objc.python_method
    def set_data(self, spans, now):
        self.spans, self.now = spans, now
        self.setNeedsDisplay_(True)

    @objc.python_method
    def colour(self, t):
        """Plain surface for setback temperatures, full heat colour from 21°."""
        f = max(0.0, min(1.0, (t - 15.0) / 6.0))
        a = rgb_tuple(SURFACE)
        b = rgb_tuple(heat_hex(t))
        return rgb("#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * f) for i in range(3)))

    def drawRect_(self, rect):
        w, h = self.bounds().size.width, self.bounds().size.height
        AK.NSGraphicsContext.saveGraphicsState()
        AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 5, 5).addClip()
        rgb(SURFACE).setFill()
        AK.NSBezierPath.fillRect_(self.bounds())
        for start, end, t in self.spans:
            self.colour(t).setFill()
            AK.NSBezierPath.fillRect_(AK.NSMakeRect(w * start / 1440.0, 0, w * (end - start) / 1440.0, h))
            rgb(BG).setFill()  # thin gap between periods
            AK.NSBezierPath.fillRect_(AK.NSMakeRect(w * start / 1440.0 - 0.5, 0, 1, h))
        AK.NSGraphicsContext.restoreGraphicsState()
        if self.now is not None:
            x = w * self.now / 1440.0
            rgb(TEXT).setFill()
            AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                AK.NSMakeRect(x - 1, 0, 2, h), 1, 1).fill()


class ScaleBar(FlippedView):
    """The target on a straight 10° to 30° scale, Braun style: ticks every 0.5°, numbers every 5°."""

    LO, HI = 10.0, 30.0

    def initWithFrame_(self, frame):
        self = objc.super(ScaleBar, self).initWithFrame_(frame)
        self.target = None  # None hides the marker (heating off)
        self.tick_hex, self.mark_hex = TEXT_3, TEXT
        return self

    @objc.python_method
    def set(self, target, tick_hex, mark_hex):
        self.target, self.tick_hex, self.mark_hex = target, tick_hex, mark_hex
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        w = self.bounds().size.width

        def x(t):
            return round((t - self.LO) / (self.HI - self.LO) * (w - 1)) + 0.5

        steps = int((self.HI - self.LO) * 2)
        for i in range(steps + 1):
            t = self.LO + i / 2.0
            major = i % 10 == 0
            rgb(self.tick_hex, 1.0 if major else 0.7).setFill()  # 0.7 keeps minor ticks over 3:1
            AK.NSRectFill(AK.NSMakeRect(x(t) - 0.5, 4, 1, 12 if major else 7))
            if major:
                text = attributed([(f"{t:g}°", 9, "regular", self.tick_hex)])
                tw = text.size().width
                left = x(t) - (0.5 if i == 0 else tw - 0.5 if i == steps else tw / 2.0)
                text.drawAtPoint_((left, 21))
        if self.target is None:
            return
        rgb(self.mark_hex).setFill()
        if self.LO <= self.target <= self.HI:
            AK.NSRectFill(AK.NSMakeRect(x(self.target) - 1.5, 0, 3, 20))
            return
        # Hive allows 5° to 32°: off the scale, a triangle at that end points the way
        low = self.target < self.LO
        edge = 0.0 if low else w
        tip = edge - 1 if low else edge + 1
        base = edge + 7 if low else edge - 7
        arrow = AK.NSBezierPath.bezierPath()
        arrow.moveToPoint_((tip, 10))
        arrow.lineToPoint_((base, 3))
        arrow.lineToPoint_((base, 17))
        arrow.closePath()
        arrow.fill()


class HistoryChart(FlippedView):
    """Room temperature over the last day, with the schedule's target behind it.

    Where the schedule asked for more than the room had, the gap is shaded amber.
    """

    def initWithFrame_(self, frame):
        self = objc.super(HistoryChart, self).initWithFrame_(frame)
        self.points = []     # [(timestamp, room temperature)]
        self.targets = []    # [(timestamp, scheduled target)], may be empty
        self.span = (0.0, 1.0)
        # colours, swapped for the orange theme while heating
        self.theme = {"grid": SEPARATOR, "label": TEXT_3, "line": TEXT, "gap": (AMBER, 0.18), "step": (AMBER, 0.8)}
        return self

    @objc.python_method
    def set_data(self, points, targets, start, end):
        self.points, self.targets, self.span = points, targets, (start, end)
        self.setNeedsDisplay_(True)

    @objc.python_method
    def y_range(self):
        values = [v for _, v in self.points] + [v for _, v in self.targets]
        lo, hi = min(values), max(values)
        lo, hi = float(int(lo)), float(int(hi) + 1)
        if hi - lo < 2:
            lo, hi = lo - 1, hi + 1
        return lo, hi

    def drawRect_(self, rect):
        if not self.points:
            return
        w, h = self.bounds().size.width, self.bounds().size.height
        plot_w = w - 26  # room on the right for the degree labels
        top, bottom = 4.0, h - 4.0
        start, end = self.span
        lo, hi = self.y_range()

        def x(ts):
            return (ts - start) / (end - start) * plot_w

        def y(v):
            return bottom - (v - lo) / (hi - lo) * (bottom - top)

        # grid: top, middle and bottom, labelled on the right
        for v in (lo, (lo + hi) / 2.0, hi):
            rgb(self.theme["grid"]).setFill()
            AK.NSRectFill(AK.NSMakeRect(0, round(y(v)), plot_w, 1))
            text = f"{v:.0f}°" if float(v).is_integer() else f"{v:.1f}°"
            label = attributed([(text, 9, "regular", self.theme["label"])])
            label.drawAtPoint_((w - label.size().width, y(v) - 6))  # right edge on the margin

        # where the schedule wanted more heat than the room had
        if self.targets:
            # one path, so the pieces blend without seams
            gap = AK.NSBezierPath.bezierPath()
            target_at = dict(self.targets)
            for (t0, v0), (t1, v1) in zip(self.points, self.points[1:]):
                want = target_at.get(t0)
                if want is not None and want > min(v0, v1):
                    gap.moveToPoint_((x(t0), y(want)))
                    gap.lineToPoint_((x(t1), y(want)))
                    gap.lineToPoint_((x(t1), y(min(v1, want))))
                    gap.lineToPoint_((x(t0), y(min(v0, want))))
                    gap.closePath()
            rgb(*self.theme["gap"]).setFill()
            gap.fill()
            # the schedule as a stepped line
            step = AK.NSBezierPath.bezierPath()
            for i, (ts, v) in enumerate(self.targets):
                if i == 0:
                    step.moveToPoint_((x(ts), y(v)))
                else:
                    step.lineToPoint_((x(ts), step.currentPoint().y))
                    step.lineToPoint_((x(ts), y(v)))
            step.lineToPoint_((plot_w, step.currentPoint().y))
            step.setLineWidth_(1.0)
            step.setLineDash_count_phase_([3.0, 3.0], 2, 0.0)
            rgb(*self.theme["step"]).setStroke()
            step.stroke()

        # the room temperature
        line = AK.NSBezierPath.bezierPath()
        for i, (ts, v) in enumerate(self.points):
            (line.moveToPoint_ if i == 0 else line.lineToPoint_)((x(ts), y(v)))
        line.setLineWidth_(1.5)
        line.setLineJoinStyle_(AK.NSLineJoinStyleRound)
        line.setLineCapStyle_(AK.NSLineCapStyleRound)
        rgb(self.theme["line"]).setStroke()
        line.stroke()
        ts, v = self.points[-1]
        rgb(self.theme["line"]).setFill()
        AK.NSBezierPath.bezierPathWithOvalInRect_(AK.NSMakeRect(x(ts) - 2.5, y(v) - 2.5, 5, 5)).fill()


class Panel(AK.NSPanel):
    owner = None

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return False

    def cancelOperation_(self, sender):  # Esc
        if self.owner is not None:
            self.owner.escape()

    def keyDown_(self, event):
        if self.owner is not None and self.owner.key_down(event):
            return
        objc.super(Panel, self).keyDown_(event)

    def performKeyEquivalent_(self, event):
        try:
            if event.type() == AK.NSEventTypeKeyDown and (
                event.modifierFlags() & AK.NSEventModifierFlagCommand
            ):
                key = event.charactersIgnoringModifiers()
                if key == "q":
                    AK.NSApp.terminate_(None)
                    return True
                edit = {"v": "paste:", "c": "copy:", "x": "cut:", "a": "selectAll:", "z": "undo:"}
                if key in edit:
                    return bool(AK.NSApp.sendAction_to_from_(edit[key], None, self))
        except Exception:  # noqa: BLE001
            pass
        return objc.super(Panel, self).performKeyEquivalent_(event)

    def resignKeyWindow(self):
        objc.super(Panel, self).resignKeyWindow()
        if self.owner is not None:
            self.owner.hide()


class ClickHandler(NSObject):
    """Target for the menu bar status item button."""

    callback = None

    def clicked_(self, sender):
        if self.callback is not None:
            self.callback()


class EditDelegate(NSObject):
    """Delegate for the inline target field: filters typing and catches Esc."""

    owner = None

    def controlTextDidChange_(self, note):
        if self.owner is not None:
            self.owner._edit_changed()

    @objc.typedSelector(b"Z@:@@:")
    def control_textView_doCommandBySelector_(self, control, text_view, selector):
        if self.owner is not None and selector in ("cancelOperation:", b"cancelOperation:"):
            self.owner.end_edit()
            return True
        return False


# ---------------------------------------------------------------- popover UI
class PopoverUI:
    def __init__(self, app):
        self.app = app
        self.pane = "control"
        self.visible = False
        self.closed_at = 0.0
        self.monitor = None
        self.state = None
        self.icon_cache_key = None
        self._handlers = []
        self.editing = False     # the target number is being typed into
        self.edit_confirm = None  # a high value waiting for a second Return
        self.edit_note = None    # (text, colour) shown under Boost while editing

        total_h = ARROW_H + BODY_H["control"]
        style = AK.NSWindowStyleMaskBorderless | AK.NSWindowStyleMaskNonactivatingPanel
        self.panel = Panel.alloc().initWithContentRect_styleMask_backing_defer_(
            AK.NSMakeRect(0, 0, POPOVER_W, total_h), style, AK.NSBackingStoreBuffered, False
        )
        self.panel.owner = self
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(AK.NSColor.clearColor())
        self.panel.setHasShadow_(True)
        # opened many times a day, so it appears and disappears instantly
        self.panel.setAnimationBehavior_(AK.NSWindowAnimationBehaviorNone)
        self.panel.setLevel_(AK.NSPopUpMenuWindowLevel)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setReleasedWhenClosed_(False)
        self.panel.setCollectionBehavior_(
            AK.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AK.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        try:
            self.panel.setAppearance_(
                AK.NSAppearance.appearanceNamed_(AK.NSAppearanceNameDarkAqua)
            )
        except Exception:  # noqa: BLE001
            pass

        self.root = FlippedView.alloc().initWithFrame_(AK.NSMakeRect(0, 0, POPOVER_W, total_h))
        self.panel.setContentView_(self.root)
        # blurred desktop behind the sign-in panes (hidden on the graphite panes)
        self.glass = AK.NSVisualEffectView.alloc().initWithFrame_(
            AK.NSMakeRect(0, ARROW_H, POPOVER_W, BODY_H["signin"])
        )
        try:
            self.glass.setMaterial_(getattr(AK, "NSVisualEffectMaterialHUDWindow", 13))
            self.glass.setBlendingMode_(0)  # behind window
            self.glass.setState_(1)         # always active
            self.glass.setWantsLayer_(True)
            self.glass.layer().setCornerRadius_(14.0)
            self.glass.layer().setMasksToBounds_(True)
        except Exception:  # noqa: BLE001
            pass
        self.glass.setHidden_(True)
        self.root.addSubview_(self.glass)
        self.bg = PopoverBackground.alloc().initWithFrame_(self.root.bounds())
        self.root.addSubview_(self.bg)

        self.control = FlippedView.alloc().initWithFrame_(
            AK.NSMakeRect(0, ARROW_H, POPOVER_W, BODY_H["control"])
        )
        self.settings = FlippedView.alloc().initWithFrame_(
            AK.NSMakeRect(0, ARROW_H, POPOVER_W, BODY_H["settings"])
        )
        self.signin = FlippedView.alloc().initWithFrame_(
            AK.NSMakeRect(0, ARROW_H, POPOVER_W, BODY_H["signin"])
        )
        self.code = FlippedView.alloc().initWithFrame_(
            AK.NSMakeRect(0, ARROW_H, POPOVER_W, BODY_H["code"])
        )
        self.schedule_pane = FlippedView.alloc().initWithFrame_(
            AK.NSMakeRect(0, ARROW_H, POPOVER_W, BODY_H["schedule"])
        )
        self.zones_pane = FlippedView.alloc().initWithFrame_(
            AK.NSMakeRect(0, ARROW_H, POPOVER_W, BODY_H["zones"])
        )
        self.panes = {
            "control": self.control,
            "settings": self.settings,
            "signin": self.signin,
            "code": self.code,
            "schedule": self.schedule_pane,
            "zones": self.zones_pane,
        }
        for view in self.panes.values():
            self.root.addSubview_(view)
        self._build_control()
        self._build_settings()
        self._build_signin()
        self._build_code()
        self._build_schedule()
        self._build_zones()
        self.show_pane("control", resize=False)

    # ------------------------------------------------------------- control pane
    def _build_control(self):
        """Laid out like a floor on the Barbican signs: big numeral left, small columns right,
        rules between rows. Colours come from self.c (see _apply_theme)."""
        c = self.control
        self.c = self._theme(False, 20.0)
        self.hot = False
        self.themed = []  # (label, text, size, weight, colour role, align): recoloured on theme change
        self.rules = []

        def label(text, x, y, w, h, size=11, role="fg", align="left", weight="regular"):
            tf = make_label([(text, size, weight, self.c[role])], x, y, w, h, align)
            self.themed.append((tf, text, size, weight, role, align))
            c.addSubview_(tf)
            return tf

        def rule(x, y, w, h=1):
            box = Box.alloc().initWithFrame_(AK.NSMakeRect(x, y, w, h))
            box.colour = rgb(self.c["rule"])
            self.rules.append(box)
            c.addSubview_(box)
            return box

        # header
        self.title_label = make_label([("Home", 11, "regular", TEXT)], 16, 16, 240 + GROW, 15)
        c.addSubview_(self.title_label)
        # the status line scrolls like a banner when it is too long to fit (see _set_status)
        self.status_clip = FlippedView.alloc().initWithFrame_(AK.NSMakeRect(16, 38, CONTENT_W - 32, 14))
        self.status_clip.setWantsLayer_(True)
        self.status_clip.layer().setMasksToBounds_(True)
        self.status_label = make_label([("", 11, "regular", TEXT_2)], 0, 0, CONTENT_W - 32, 14)
        self.status_clip.addSubview_(self.status_label)
        c.addSubview_(self.status_clip)
        self.marquee_overflow = 0.0
        self.marquee_t0 = 0.0
        self.marquee_timer = None
        self.settings_btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(276 + GROW, 19, 28, 28))
        self.settings_btn.configure(4, None, HOVER)
        self.settings_btn.callback = lambda: self.show_pane("settings")
        self.settings_btn.ax_label = "Settings"
        self.settings_icon = icon_view("ellipsis", TEXT_2, 16, 6, 6)
        self.settings_btn.addSubview_(self.settings_icon)
        c.addSubview_(self.settings_btn)
        rule(16, 62, CONTENT_W)

        # the big numeral; render() sizes it to fit NUMERAL_W
        self.number_group = FlippedView.alloc().initWithFrame_(AK.NSMakeRect(16, 72, NUMERAL_W, 104))
        self.target_label = make_label([("--", NUMERAL_PT, "heavy-display", TEXT)], 0, 0, NUMERAL_W, 120)
        self.degree_label = make_label([("°", 36, "heavy-display", TEXT)], 0, 0, 0, 0)
        for view in (self.target_label, self.degree_label):
            self.number_group.addSubview_(view)
        c.addSubview_(self.number_group)

        # columns to the right: Current, Outside, then Mode (or the boost countdown)
        x, w = 216, 128  # room for "Boost" and "42:09 left" on one line
        # one line each, label left and value right: Current, Outside, Mode (or Boost), Hot water
        def row_y(i):
            return 80 + 26 * i
        label("Current", x, row_y(0), w, 14, role="fg3")
        self.current_label = make_label([("--", 11, "regular", TEXT)], x, row_y(0), w, 14, "right")
        c.addSubview_(self.current_label)
        self.outside_key = label("Outside", x, row_y(1), w, 14, role="fg3")
        self.outside_group = FlippedView.alloc().initWithFrame_(AK.NSMakeRect(x, row_y(1), w, 14))
        self.outside_icon = icon_view("cloud", TEXT, 12, 0, 1)  # moved left of the value by _render_weather
        self.outside_label = make_label([("", 11, "regular", TEXT)], 0, 0, w, 14, "right")
        self.outside_group.addSubview_(self.outside_icon)
        self.outside_group.addSubview_(self.outside_label)
        self.outside_key.setHidden_(True)
        self.outside_group.setHidden_(True)
        c.addSubview_(self.outside_group)
        self.weather = None  # (temperature, description) once Hive has answered
        self.mode_key = make_label([("Mode", 11, "regular", TEXT_3)], x, row_y(2), w, 14)
        c.addSubview_(self.mode_key)
        self.countdown_label = make_label([("", 11, "regular", TEXT)], x, row_y(2), w, 14, "right")
        c.addSubview_(self.countdown_label)
        # hot water, only on accounts with a tank (see hot_water_state)
        self.water_key = label("Hot water", x, row_y(3), w, 14, role="fg3")
        self.water_label = make_label([("", 11, "regular", TEXT)], x, row_y(3), w, 14, "right")
        c.addSubview_(self.water_label)
        self.water_key.setHidden_(True)
        self.water_label.setHidden_(True)

        # "Set temperature" types a target in the number's place; the number itself isn't clickable
        self.target_field = self._number_field()
        c.addSubview_(self.target_field)

        # the target on a straight scale
        self.scale = ScaleBar.alloc().initWithFrame_(AK.NSMakeRect(16, 197, CONTENT_W, 34))
        c.addSubview_(self.scale)
        rule(16, 240, CONTENT_W)

        # − | Set temperature | +, cells split by rules (Temperature controls, 3:841)
        self.minus_btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 241, 56, 48))
        self.minus_btn.callback = lambda: (self.end_edit(), self.app.nudge(-STEP))
        self.minus_btn.ax_label = "Lower target"
        self.minus_icon = icon_view("minus", TEXT, 16, 20, 16)
        self.minus_btn.addSubview_(self.minus_icon)
        c.addSubview_(self.minus_btn)
        rule(16, 241, 1, 48)  # the row is boxed: rules at both ends too
        rule(72, 241, 1, 48)
        self.set_btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(73, 241, 174 + GROW, 48))
        self.set_btn.callback = self.begin_edit
        self.set_text = make_label([("Set temperature", 11, "regular", TEXT)], 0, 17, 174 + GROW, 14, "center")
        self.set_btn.addSubview_(self.set_text)
        c.addSubview_(self.set_btn)
        rule(247 + GROW, 241, 1, 48)
        rule(343, 241, 1, 48)
        self.plus_btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(248 + GROW, 241, 56, 48))
        self.plus_btn.callback = lambda: (self.end_edit(), self.app.nudge(STEP))
        self.plus_btn.ax_label = "Raise target"
        self.plus_icon = icon_view("plus", TEXT, 16, 20, 16)
        self.plus_btn.addSubview_(self.plus_icon)
        c.addSubview_(self.plus_btn)
        rule(16, 289, CONTENT_W)

        # boost: label left, timer right
        self.boost_btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 290, CONTENT_W, 48))
        self.boost_btn.callback = lambda: (self.end_edit(), self.app.toggle_boost())
        self.boost_text = make_label([("Boost for 1 hour", 11, "regular", TEXT)], 0, 17, 240 + GROW, 14)  # on the margin, like the rows above
        self.boost_btn.addSubview_(self.boost_text)
        self.boost_icon = icon_view("timer", TEXT, 15, 273 + GROW, 16.5)  # right edge on the margin
        self.boost_btn.addSubview_(self.boost_icon)
        self.boost_btn.on_active = self._inset_boost  # on hover or focus, ease in off the edges
        c.addSubview_(self.boost_btn)
        rule(16, 338, CONTENT_W)
        self.note_label = make_label([("", 10, "regular", TEXT_3)], 16, NOTE_Y, CONTENT_W, 13, wrap=True)
        c.addSubview_(self.note_label)

        # the last day
        label("Last 24 hours", 16, 370, 140, 13, size=10, role="fg3")
        self.history_summary = make_label([("", 10, "regular", TEXT_3)], 16 + math.ceil(text_width([("Last 24 hours", 10, "regular", TEXT_3)])) - 2, 370, 220, 13)  # Low and High follow it after a dot
        c.addSubview_(self.history_summary)
        self.history_chart = HistoryChart.alloc().initWithFrame_(AK.NSMakeRect(16, 391, CONTENT_W, 128))
        c.addSubview_(self.history_chart)
        self.history_note = make_label([("Loading…", 10, "regular", TEXT_3)], 16, 447, CONTENT_W - 26, 13, "center")
        c.addSubview_(self.history_note)
        self.history_hours = []
        for _ in range(5):
            hour = make_label([("", 9, "regular", TEXT_3)], 0, HISTORY_HOURS_Y, 48, 11, "center")
            self.history_hours.append(hour)
            c.addSubview_(hour)
        self._apply_theme(False, 20.0)

    BOOST_INSET = 8  # how far the boost text and icon move in from the edges on hover

    @objc.python_method
    def _inset_boost(self, active):
        """Ease the boost label and icon in from the row's edges while it is hovered or focused,
        so they sit inside the hover fill (and clear of the focus ring) instead of on its edge."""
        d = self.BOOST_INSET if active else 0
        AK.NSAnimationContext.beginGrouping()
        ctx = AK.NSAnimationContext.currentContext()
        ctx.setDuration_(0.0 if reduce_motion() else 0.18)
        ctx.setTimingFunction_(objc.lookUpClass("CAMediaTimingFunction").functionWithName_("easeOut"))
        self.boost_text.animator().setFrameOrigin_((0 + d, 17))
        self.boost_icon.animator().setFrameOrigin_((273 + GROW - d, 16.5))
        AK.NSAnimationContext.endGrouping()

    # ------------------------------------------------------------- colour themes
    @staticmethod
    def _theme(hot, target):
        """Colour roles for the control pane. hot: heating, so the pane is orange."""
        if not hot:
            return {"bg": BG, "border": BORDER, "fg": TEXT, "fg2": TEXT_2, "fg3": TEXT_3,
                    "rule": SEPARATOR, "hover": HOVER, "pressed": None, "numeral": TEXT, "mark": TEXT, "accent": heat_hex(target),
                    "chart": {"grid": SEPARATOR, "label": TEXT_3, "line": TEXT,
                              "gap": (AMBER, 0.18), "step": (AMBER, 0.8)}}
        # orange, shading to red over the same range as amber does
        f = max(0.0, min(1.0, (float(target) - WARM_TEMP) / (HOT_TEMP - WARM_TEMP)))
        a, b = rgb_tuple(ORANGE), rgb_tuple(ORANGE_RED)
        bg = "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * f) for i in range(3))
        def shade(k):
            return "#%02x%02x%02x" % tuple(round(v * k) for v in rgb_tuple(bg))
        darker, pressed = shade(0.88), shade(0.80)  # hover, then pressed: both darker than the pane
        return {"bg": bg, "border": darker, "fg": ON_ORANGE, "fg2": ON_ORANGE_2, "fg3": ON_ORANGE_2,
                "rule": ON_ORANGE, "hover": darker, "pressed": pressed, "numeral": "#ffffff", "mark": "#ffffff",
                "accent": ON_ORANGE,
                "chart": {"grid": ON_ORANGE, "label": ON_ORANGE_2, "line": ON_ORANGE,
                          "gap": ("#000000", 0.16), "step": (ON_ORANGE, 0.7)}}

    def _apply_theme(self, hot, target):
        """Recolour the control pane's fixed parts. Changing parts are coloured by render()."""
        changed = hot != getattr(self, "hot", None)
        self.hot = hot
        self.c = c = self._theme(hot, target)
        for tf, text, size, weight, role, align in self.themed:
            set_label(tf, [(text, size, weight, c[role])], align)
        for box in self.rules:
            box.colour = rgb(c["rule"])
            box.setNeedsDisplay_(True)
        for btn in (self.minus_btn, self.set_btn, self.plus_btn, self.boost_btn):
            btn.configure(0, None, c["hover"], pressed=c["pressed"])
            btn.focus_hex = c["fg"]
        self.settings_btn.configure(4, None, c["hover"], pressed=c["pressed"])
        self.settings_btn.focus_hex = c["fg"]
        set_icon(self.settings_icon, "ellipsis", c["fg2"], 16)
        set_icon(self.minus_icon, "minus", c["fg"], 16)
        set_icon(self.plus_icon, "plus", c["fg"], 16)
        set_label(self.set_text, [("Set temperature", 11, "regular", c["fg"])], "center")
        set_label(self.title_label, [(plain_text(self.title_label) or "Home", 11, "regular", c["fg"])])
        self.history_chart.theme = c["chart"]
        self.history_chart.setNeedsDisplay_(True)
        self.target_field.setTextColor_(rgb(c["numeral"]))
        self._paint_bg()
        if changed and getattr(self, "_history_args", None):
            self.set_history(*self._history_args)

    def _paint_bg(self):
        """Orange behind the control pane while heating, graphite everywhere else."""
        on_control = self.pane == "control" and getattr(self, "hot", False)
        self.bg.fill_hex = self.c["bg"] if on_control else BG
        self.bg.stroke_hex = self.c["border"] if on_control else BORDER
        self.bg.setNeedsDisplay_(True)

    # ------------------------------------------------------------- typing a target
    EDIT_HINT = ("Type a temperature, then press Return. Esc cancels.", TEXT_3)  # colour is for the graphite pane

    def _number_field(self):
        """Editable twin of the big target number, shown in its place while typing."""
        tf = AK.NSTextField.alloc().initWithFrame_(AK.NSMakeRect(14, 62, NUMERAL_W + 4, 92))
        tf.setBezeled_(False)
        tf.setBordered_(False)
        tf.setDrawsBackground_(False)
        tf.setEditable_(True)
        tf.setSelectable_(True)
        tf.setFocusRingType_(AK.NSFocusRingTypeNone)
        tf.setAlignment_(AK.NSTextAlignmentLeft)
        f = font(NUMERAL_PT, "heavy-display")  # the same size and place as the number it stands in for
        top = 8 - (f.ascender() - f.capHeight())
        tf.setFrame_(AK.NSMakeRect(16 - 2, 72 + top, NUMERAL_W + 4, f.ascender() - f.descender() + 4))
        tf.setFont_(f)
        tf.setAllowsEditingTextAttributes_(True)  # keeps the number's tight letter spacing while typing
        tf.setTextColor_(rgb(TEXT))
        tf.cell().setSendsActionOnEndEditing_(False)
        handler = ClickHandler.alloc().init()
        handler.callback = self._commit_edit
        self._handlers.append(handler)
        tf.setTarget_(handler)
        tf.setAction_("clicked:")
        self.edit_delegate = EditDelegate.alloc().init()
        self.edit_delegate.owner = self
        tf.setDelegate_(self.edit_delegate)
        tf.setHidden_(True)
        return tf

    def begin_edit(self):
        st = self.state
        if not st or st["mode"] == "OFF" or not self.set_btn.enabled:
            return
        if not self.editing:
            self.editing = True
            self.edit_confirm = None
            t = float(st["target"])
            self.target_field.setAttributedStringValue_(self._edit_text(f"{int(t)}" if t.is_integer() else f"{t:.1f}"))
            self.target_label.setHidden_(True)  # the field stands in for the digits; the ° stays
            self.target_field.setHidden_(False)
            self._place_edit_degree()
            self._set_edit_note(self.EDIT_HINT)
        try:
            AK.NSApp.activateIgnoringOtherApps_(True)  # typing needs the app to be active
        except Exception:  # noqa: BLE001
            pass
        self.panel.makeFirstResponder_(self.target_field)
        self._select_edit()

    def end_edit(self):
        """Leave typing without setting anything."""
        if not self.editing:
            return
        self.editing = False
        self.edit_confirm = None
        self.edit_note = None
        self.target_field.setHidden_(True)
        self.target_label.setHidden_(False)
        # after Return the field is still busy, so drop focus a moment later
        AppHelper.callAfter(self._drop_edit_focus)
        if self.state:
            self._render_boost(self.state)

    def _drop_edit_focus(self):
        if not self.editing:
            self.panel.makeFirstResponder_(self.panel)

    @objc.python_method
    def _edit_attrs(self, text=""):
        """The big number's look (size, -8% spacing, colour) for the typing field, smaller for "21.5" as on screen."""
        size = NUMERAL_PT
        while size > 48 and (text_width([(text, size, "heavy-display", TEXT, -0.08 * size)])
                             + text_width([("°", size * DEGREE_RATIO, "heavy-display", TEXT)])) > NUMERAL_W:
            size -= 4  # the same fit as _set_number, ° included
        return {AK.NSFontAttributeName: font(size, "heavy-display"),
                AK.NSKernAttributeName: -0.08 * size,
                AK.NSForegroundColorAttributeName: rgb(self.c["numeral"])}

    @objc.python_method
    def _place_edit_degree(self):
        """While typing, the ° follows the typed digits at their size, as it follows the number."""
        text = str(self.target_field.stringValue())
        size = self._edit_attrs(text)[AK.NSFontAttributeName].pointSize()
        f = font(size, "heavy-display")  # keep the digit tops where the number's are, at any size
        self.target_field.setFrame_(AK.NSMakeRect(16 - 2, 72 + 8 - (f.ascender() - f.capHeight()), NUMERAL_W + 4,
                                                  f.ascender() - f.descender() + 4))
        num_w = text_width([(text, size, "heavy-display", TEXT, -0.08 * size)]) if text else 0
        deg = [("°", size * DEGREE_RATIO, "heavy-display", self.c["numeral"])]
        df = font(size * DEGREE_RATIO, "heavy-display")
        set_label(self.degree_label, deg)
        self.degree_label.setHidden_(False)
        self.degree_label.setFrame_(AK.NSMakeRect(num_w + 2, 8 - (df.ascender() - df.capHeight()), text_width(deg) + 6,
                                                  df.ascender() - df.descender() + 4))

    @objc.python_method
    def _edit_text(self, text):
        return AK.NSAttributedString.alloc().initWithString_attributes_(text, self._edit_attrs(text))

    def _select_edit(self):
        self.target_field.selectText_(None)
        editor = self.target_field.currentEditor()
        if editor is not None:  # amber caret and selection instead of the system blue
            attrs = self._edit_attrs(str(self.target_field.stringValue()))
            editor.textStorage().setAttributes_range_(attrs, AK.NSMakeRange(0, editor.textStorage().length()))
            editor.setTypingAttributes_(attrs)
            caret = ON_ORANGE if self.hot else AMBER  # amber, or black on the orange pane
            editor.setInsertionPointColor_(rgb(caret))
            editor.setSelectedTextAttributes_({AK.NSBackgroundColorAttributeName: rgb(caret, 0.28)})

    @objc.python_method
    def _set_note(self, parts):
        """The note under Boost; a second line moves it up so it stays clear of Last 24 hours."""
        set_label(self.note_label, parts, wrap=True)
        h = self.note_label.cell().cellSizeForBounds_(AK.NSMakeRect(0, 0, CONTENT_W, 100)).height
        two = h > 16
        self.note_label.setFrame_(AK.NSMakeRect(16, NOTE_Y - 7 if two else NOTE_Y, CONTENT_W, 26 if two else 13))

    def _set_edit_note(self, note):
        self.edit_note = note
        colour = self.c["fg"] if self.hot else note[1]  # on orange, all notes are black
        self._set_note([(note[0], 10, "regular", colour)])

    def _edit_changed(self):
        """Keep to digits and one decimal mark, at most 4 characters."""
        text = str(self.target_field.stringValue())
        clean = "".join(ch for ch in text if ch.isdigit() or ch in ".,")[:4]
        editor = self.target_field.currentEditor()
        attrs = self._edit_attrs(clean)
        if editor is not None:  # restyle in place, so the caret stays where it is
            sel = editor.selectedRange()
            if clean != text:
                editor.setString_(clean)
                sel = AK.NSMakeRange(len(clean), 0)
            editor.textStorage().setAttributes_range_(attrs, AK.NSMakeRange(0, len(clean)))
            editor.setTypingAttributes_(attrs)
            editor.setSelectedRange_(sel)
        elif clean != text:
            self.target_field.setAttributedStringValue_(self._edit_text(clean))
        self._place_edit_degree()
        if self.edit_note != self.EDIT_HINT:
            self.edit_confirm = None
            self._set_edit_note(self.EDIT_HINT)

    def _commit_edit(self):
        """Return: set the typed value. High values need a second Return."""
        if not self.editing:
            return
        text =str(self.target_field.stringValue()).strip().replace(",", ".")
        try:
            temp = float(text)
        except ValueError:
            self._set_edit_note(("Enter a number, like 20.5.", ERROR))
            self._select_edit()
            return
        if not MIN_TEMP <= temp <= MAX_TEMP:
            self._set_edit_note((f"Hive allows {MIN_TEMP:g}° to {MAX_TEMP:g}°.", ERROR))
            self._select_edit()
            return
        temp = round(temp * 2) / 2
        if temp >= CONFIRM_TEMP and self.edit_confirm != temp:
            self.edit_confirm = temp
            self._set_edit_note((f"{temp:g}° is very warm. Press Return again to set it.", RED))
            self._select_edit()
            return
        self.end_edit()
        self.app.set_target(temp)

    # ------------------------------------------------------------- settings pane
    def _separator(self, y):
        box = Box.alloc().initWithFrame_(AK.NSMakeRect(16, y, CONTENT_W, 1))
        box.colour = rgb(SEPARATOR)
        return box

    def _build_settings(self):
        s = self.settings
        back = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 19.5, 24, 28))
        back.configure(6, None, SURFACE)
        back.callback = lambda: self.show_pane("control")
        back.ax_label = "Back"
        back.addSubview_(icon_view("chevron-left", TEXT, 16, 4, 6))
        s.addSubview_(back)
        s.addSubview_(make_label([("Settings", 14, "semibold", TEXT)], 50, 25, 200, 17))
        s.addSubview_(make_label([(f"{APP_NAME} {VERSION}", 10, "code-regular", TEXT_3)], 174 + GROW, 27.5, 120, 12, "right"))
        s.addSubview_(self._separator(63))

        s.addSubview_(make_label([("Mode", 10, "code-regular", TEXT_3)], 16, 76, CONTENT_W, 12))
        self.mode_rows = {}
        modes = (
            ("SCHEDULE", "Scheduled", "Follow your thermostat schedule", "calendar-clock"),
            ("MANUAL", "Manual", "Hold a set temperature", "hand"),
            ("OFF", "Off", "Turn heating off", "power"),
        )
        for i, (key, name, desc, icon) in enumerate(modes):
            row = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 94 + 53 * i, CONTENT_W, 51))
            row.configure(4, None, ROW_HOVER)
            row.callback = (lambda k=key: self.app.set_mode(k))
            row_icon = icon_view(icon, TEXT_2, 16, 10, 17.5)
            row.addSubview_(row_icon)
            name_label = make_label([(name, 12, "regular", TEXT)], 36, 10, 200, 15)
            row.addSubview_(name_label)
            row.addSubview_(make_label([(desc, 10, "regular", TEXT_3)], 36, 29, 220 + GROW, 12))
            check = icon_view("check", TEXT, 14, 264 + GROW, 18.5)
            row.addSubview_(check)
            s.addSubview_(row)
            self.mode_rows[key] = (row, row_icon, icon, name_label, name, check)

        s.addSubview_(self._separator(263))

        # only shown when the account has more than one thermostat
        self.zone_row = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 276, CONTENT_W, 34))
        self.zone_row.configure(4, None, ROW_HOVER)
        self.zone_row.callback = lambda: self.show_pane("zones")
        self.zone_row.addSubview_(icon_view("layers", TEXT_2, 15, 10, 9.5))
        self.zone_row.addSubview_(make_label([("Zone", 12, "regular", TEXT)], 35, 9.5, 80, 15))
        self.zone_now_label = make_label([("", 10, "regular", TEXT_3)], 100, 11, 178 + GROW, 12, "right")
        self.zone_row.addSubview_(self.zone_now_label)
        self.zone_row.setHidden_(True)
        s.addSubview_(self.zone_row)
        self.zone_names = []
        self.zone_index = 0
        self.zone_shifted = []  # (view, y) for everything under the zone row

        schedule_row = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 276, CONTENT_W, 34))
        schedule_row.configure(4, None, ROW_HOVER)
        schedule_row.callback = lambda: self.show_pane("schedule")
        schedule_row.addSubview_(icon_view("calendar-days", TEXT_2, 15, 10, 9.5))
        schedule_row.addSubview_(make_label([("Schedule", 12, "regular", TEXT)], 35, 9.5, 120, 15))
        self.schedule_now_label = make_label([("", 10, "regular", TEXT_3)], 120, 11, 158 + GROW, 12, "right")
        schedule_row.addSubview_(self.schedule_now_label)
        s.addSubview_(schedule_row)

        refresh = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 310, CONTENT_W, 34))
        refresh.configure(4, None, ROW_HOVER)
        refresh.callback = lambda: self.app.refresh(user=True)
        refresh.addSubview_(icon_view("refresh-cw", TEXT_2, 15, 10, 9.5))
        refresh.addSubview_(make_label([("Refresh", 12, "regular", TEXT)], 35, 9.5, 149, 15))
        self.updated_label = make_label([("", 10, "regular", TEXT_3)], 144, 11, 134 + GROW, 12, "right")
        refresh.addSubview_(self.updated_label)
        s.addSubview_(refresh)

        # updates: the row installs a newer release when there is one, else checks now
        self.update_row = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 344, CONTENT_W, 34))
        self.update_row.configure(4, None, ROW_HOVER)
        self.update_row.callback = lambda: self.app.update_clicked()
        self.update_row.addSubview_(icon_view("download", TEXT_2, 15, 10, 9.5))
        self.update_title = make_label([("Updates", 12, "regular", TEXT)], 35, 9.5, 140, 15)
        self.update_row.addSubview_(self.update_title)
        self.update_label = make_label([("", 10, "regular", TEXT_3)], 120, 11, 158 + GROW, 12, "right")
        self.update_row.addSubview_(self.update_label)
        s.addSubview_(self.update_row)

        auto = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 378, CONTENT_W, 34))
        auto.configure(4, None, ROW_HOVER)
        auto.callback = lambda: self.app.toggle_auto_update()
        auto.addSubview_(make_label([("Check for updates automatically", 12, "regular", TEXT)], 35, 9.5, 230 + GROW, 15))
        self.auto_check = icon_view("check", TEXT, 14, 264 + GROW, 10)
        auto.addSubview_(self.auto_check)
        self.auto_row = auto
        s.addSubview_(auto)

        s.addSubview_(self._separator(424))

        sign_out = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 437, CONTENT_W, 34))
        sign_out.configure(4, None, ROW_HOVER)
        sign_out.callback = lambda: self.app.sign_out()
        sign_out.addSubview_(icon_view("log-out", TEXT_2, 15, 10, 9.5))
        sign_out.addSubview_(make_label([("Sign out", 12, "regular", TEXT)], 35, 9.5, 243 + GROW, 15))
        s.addSubview_(sign_out)

        quit_row = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 471, CONTENT_W, 34))
        quit_row.configure(4, None, ROW_HOVER)
        quit_row.callback = lambda: AK.NSApp.terminate_(None)
        quit_row.addSubview_(icon_view("power", TEXT_2, 15, 10, 9.5))
        quit_row.addSubview_(make_label([(f"Quit {APP_NAME}", 12, "regular", TEXT)], 35, 9.5, 215 + GROW, 15))
        quit_row.addSubview_(make_label([("⌘Q", 10, "regular", TEXT_3)], 228 + GROW, 11, 50, 12, "right"))
        s.addSubview_(quit_row)
        self.zone_shifted = [(v, v.frame().origin.y) for v in s.subviews()
                             if v.frame().origin.y >= 276 and v is not self.zone_row]
        self.settings_base_h = BODY_H["settings"]

    # ---------------------------------------------------------------- zones pane
    def _build_zones(self):
        p = self.zones_pane
        back = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 19.5, 24, 28))
        back.configure(6, None, SURFACE)
        back.callback = lambda: self.show_pane("settings")
        back.ax_label = "Back"
        back.addSubview_(icon_view("chevron-left", TEXT, 16, 4, 6))
        p.addSubview_(back)
        p.addSubview_(make_label([("Zone", 14, "semibold", TEXT)], 50, 25, 200, 17))
        p.addSubview_(self._separator(63))
        self.zone_rows = []

    def set_zones(self, names, index):
        """names: one per thermostat on the account; index: the one Ember is showing."""
        self.zone_names, self.zone_index = list(names), index
        many = len(names) > 1
        name = names[index] if names else ""
        set_label(self.title_label, [(name if many else "Home", 11, "regular", self.c["fg"])])
        set_label(self.zone_now_label, [(name, 10, "regular", TEXT_3)], "right")
        self.zone_row.setHidden_(not many)
        shift = 34 if many else 0
        for view, y in self.zone_shifted:
            frame = view.frame()
            view.setFrameOrigin_(AK.NSMakePoint(frame.origin.x, y + shift))
        BODY_H["settings"] = self.settings_base_h + shift
        self.settings.setFrameSize_(AK.NSMakeSize(POPOVER_W, BODY_H["settings"]))

        p = self.zones_pane
        for row, *_ in self.zone_rows:
            row.removeFromSuperview()
        self.zone_rows = []
        for i, zone in enumerate(names):
            row = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 76 + 34 * i, CONTENT_W, 34))
            row.configure(4, None, ROW_HOVER)
            row.callback = (lambda k=i: self.app.select_zone(k))
            chosen = i == index
            row.ax_selected = chosen
            row.addSubview_(make_label([(zone, 12, "medium" if chosen else "regular", TEXT)], 10, 9.5, 240 + GROW, 15))
            if chosen:
                row.addSubview_(icon_view("check", TEXT, 14, 264 + GROW, 10))
            p.addSubview_(row)
            self.zone_rows.append((row,))
        BODY_H["zones"] = 76 + 34 * max(1, len(names)) + 16
        p.setFrameSize_(AK.NSMakeSize(POPOVER_W, BODY_H["zones"]))
        if self.pane in ("settings", "zones"):
            self.show_pane(self.pane)

    # ------------------------------------------------------------- schedule pane
    SCHEDULE_ROWS = 8      # most periods shown for one day
    SCHEDULE_TOP = 176     # first period row
    SCHEDULE_STEP = 34

    def _build_schedule(self):
        p = self.schedule_pane
        back = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 19.5, 24, 28))
        back.configure(6, None, SURFACE)
        back.callback = lambda: self.show_pane("settings")
        back.ax_label = "Back"
        back.addSubview_(icon_view("chevron-left", TEXT, 16, 4, 6))
        p.addSubview_(back)
        p.addSubview_(make_label([("Schedule", 14, "semibold", TEXT)], 50, 25, 200, 17))
        p.addSubview_(self._separator(63))

        self.schedule = None
        self.schedule_day = datetime.now().weekday()
        self.day_btns = []
        gap = 4.0
        w = (CONTENT_W - gap * 6) / 7.0
        for i, name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(16 + i * (w + gap), 76, w, 28))
            btn.configure(6, None, ROW_HOVER)
            btn.callback = (lambda d=i: self.show_day(d))
            label = make_label([(name, 11, "regular", TEXT_2)], 0, 7, w, 14, "center")
            btn.addSubview_(label)
            p.addSubview_(btn)
            self.day_btns.append((btn, label, name))

        self.day_bar = DayBar.alloc().initWithFrame_(AK.NSMakeRect(16, 118, CONTENT_W, 20))
        p.addSubview_(self.day_bar)
        for hour in (0, 6, 12, 18, 24):
            x = 16 + CONTENT_W * hour / 24.0
            align, left = ("left", x) if hour == 0 else ("right", x - 40) if hour == 24 else ("center", x - 20)
            p.addSubview_(make_label([(f"{hour:02d}:00", 9, "mono-regular", TEXT_3)], left, 143, 40, 11, align))
        p.addSubview_(self._separator(164))

        self.period_rows = []
        for i in range(self.SCHEDULE_ROWS):
            row = Box.alloc().initWithFrame_(
                AK.NSMakeRect(16, self.SCHEDULE_TOP + i * self.SCHEDULE_STEP, CONTENT_W, 32))
            row.setWantsLayer_(True)
            row.layer().setCornerRadius_(8)
            row.layer().setMasksToBounds_(True)
            start = make_label([("", 12, "mono-regular", TEXT)], 10, 8.5, 60, 15)
            until = make_label([("", 10, "regular", TEXT_3)], 62, 10, 150 + GROW, 13)
            temp = make_label([("", 12, "medium", TEXT)], 200 + GROW, 8.5, 78, 15, "right")
            for view in (start, until, temp):
                row.addSubview_(view)
            p.addSubview_(row)
            self.period_rows.append((row, start, until, temp))
        self.schedule_empty = make_label(
            [("Hive hasn't sent a schedule yet", 11, "regular", TEXT_3)], 16, self.SCHEDULE_TOP + 8, CONTENT_W, 14, "center")
        p.addSubview_(self.schedule_empty)
        self.schedule_note = make_label(
            [("To change it, use the Hive app for now", 10, "regular", TEXT_3)], 16, 0, CONTENT_W, 12, "center")
        p.addSubview_(self.schedule_note)

    def show_day(self, day):
        self.schedule_day = day % 7
        self._render_schedule()

    def set_schedule(self, schedule):
        if schedule == self.schedule:
            return
        self.schedule = copy.deepcopy(schedule)
        # one height for the whole week, so switching days doesn't make the popover jump
        most = max([len(day_slots(schedule, d)) for d in range(7)] + [1])
        rows = min(most, self.SCHEDULE_ROWS)
        note_y = self.SCHEDULE_TOP + rows * self.SCHEDULE_STEP + 6
        self.schedule_note.setFrameOrigin_((16, note_y))
        height = note_y + 12 + 18
        if BODY_H["schedule"] != height:
            BODY_H["schedule"] = height
            self.schedule_pane.setFrameSize_((POPOVER_W, height))
            if self.pane == "schedule":
                self.show_pane("schedule")
        self._render_schedule()

    def _periods(self, day):
        """[(start, end, target)] covering the whole day; end may be the next day's first start."""
        slots = day_slots(self.schedule, day)
        if not slots:
            return []
        out = []
        for i, (start, t) in enumerate(slots):
            end = slots[i + 1][0] if i + 1 < len(slots) else None
            out.append((start, end, t))
        return out

    def _render_schedule(self):
        day = self.schedule_day
        today = datetime.now().weekday()
        for i, (btn, label, name) in enumerate(self.day_btns):
            selected = i == day
            btn.fill = rgb(SURFACE) if selected else None
            btn.ax_selected = selected
            btn.setNeedsDisplay_(True)
            colour = AMBER if i == today else TEXT if selected else TEXT_2
            set_label(label, [(name, 11, "medium" if selected else "regular", colour)], "center")

        periods = self._periods(day)
        now = datetime.now()
        now_min = now.hour * 60 + now.minute if day == today else None
        # the strip: before the first period, yesterday's last one is still running
        spans = []
        if periods:
            prev = day_slots(self.schedule, day - 1)
            first = periods[0][0]
            if first > 0 and prev:
                spans.append((0, first, prev[-1][1]))
            spans += [(st, end if end is not None else 1440, t) for st, end, t in periods]
        self.day_bar.set_data(spans, now_min)

        next_first = day_slots(self.schedule, day + 1)
        tomorrow = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[(day + 1) % 7]
        current = None
        if now_min is not None:
            for i, (st, end, t) in enumerate(periods):
                if st <= now_min:
                    current = i
        for i, (row, start, until, temp) in enumerate(self.period_rows):
            if i >= len(periods):
                row.setHidden_(True)
                continue
            st, end, t = periods[i]
            row.setHidden_(False)
            here = i == current
            row.colour = rgb(AMBER_SURFACE) if here else None
            row.setNeedsDisplay_(True)
            if end is not None:
                till = f"Until {clock(end)}"
            elif next_first:
                till = f"Until {tomorrow} {clock(next_first[0][0])}"
            else:
                till = ""
            if here:
                till = "Now · " + till
            set_label(start, [(clock(st), 12, "mono-regular", TEXT)])
            set_label(until, [(till, 10, "regular", AMBER if here else TEXT_3)])
            set_label(temp, [(temp_text(t), 12, "medium", heat_hex(t) if here else TEXT)], "right")
        self.schedule_empty.setHidden_(bool(periods))

        # the Settings row says what the schedule wants right now
        text = ""
        if self.schedule:
            cur = self._periods(today)
            mins = now.hour * 60 + now.minute
            past = [p for p in cur if p[0] <= mins]
            if past:
                text = f"Now {temp_text(past[-1][2])}"
            else:
                prev = day_slots(self.schedule, today - 1)
                text = f"Now {temp_text(prev[-1][1])}" if prev else ""
        set_label(self.schedule_now_label, [(text, 10, "regular", TEXT_3)], "right")

    def set_weather(self, temp, description):
        """Outside temperature under Current, or hidden if Hive had no answer."""
        self.weather = None if temp is None else (temp, description)
        self._render_weather()

    def _render_weather(self):
        if self.weather is None or getattr(self, "state", None) is None:
            self.outside_key.setHidden_(True)
            self.outside_group.setHidden_(True)
            return
        temp, description = self.weather
        set_icon(self.outside_icon, weather_icon(description, is_night()), self.c["numeral"], 12)  # values are white on orange
        parts = [(f"{temp:.1f}°C", 11, "regular", self.c["numeral"])]
        set_label(self.outside_label, parts, "right")
        w = self.outside_group.frame().size.width
        self.outside_icon.setFrameOrigin_((w - text_width(parts) - 2 - 16, 1))
        self.outside_group.setToolTip_(description.capitalize() if description else None)
        set_plain(self.outside_label, f"Outside {temp:.1f}°C" + (f", {description}" if description else ""))
        self.outside_key.setHidden_(False)
        self.outside_group.setHidden_(False)

    # ------------------------------------------------------------- panes and window
    def _clear_hover(self, view=None):
        """Hidden views get no mouse-exited event, so hover would stick. Reset it."""
        view = view or self.root
        if isinstance(view, Hit) and (view.hovering or view.pressed):
            view.hovering = view.pressed = False
            view.setNeedsDisplay_(True)
        for sub in view.subviews():
            self._clear_hover(sub)

    def escape(self):
        """Esc stops typing, steps back a pane, and closes the popover from the first one."""
        if self.editing:
            self.end_edit()
        elif self.pane == "settings":
            self.show_pane("control")
        elif self.pane in ("schedule", "zones"):
            self.show_pane("settings")
        elif self.pane == "code":
            self._cancel_code()
        else:
            self.hide()

    def key_down(self, event):
        """Keys no control took: Tab moves between buttons, up and down change the target."""
        key = event.charactersIgnoringModifiers()
        shift = bool(event.modifierFlags() & AK.NSEventModifierFlagShift)
        if key in ("\t", "\x19"):
            self.focus_next(back=shift or key == "\x19")
            return True
        arrows = {"\uf700": STEP, "\uf701": -STEP}  # up and down arrow keys
        if key in arrows and self.pane == "control" and not self.editing:
            self.app.nudge(arrows[key])
            return True
        return False

    def focus_next(self, back=False):
        def hits(view):
            for sub in view.subviews():
                if sub.isHidden():
                    continue
                if isinstance(sub, Hit) and sub.callback is not None and sub.enabled:
                    yield sub
                yield from hits(sub)
        order = sorted(hits(self.panes[self.pane]), key=lambda v: (
            round(v.convertPoint_toView_(AK.NSMakePoint(0, 0), None).y * -1), v.convertPoint_toView_(AK.NSMakePoint(0, 0), None).x))
        if not order:
            return
        current = self.panel.firstResponder()
        i = order.index(current) if current in order else (-1 if not back else len(order))
        target = order[(i - 1 if back else i + 1) % len(order)]
        Hit.tabbing = True
        try:
            self.panel.makeFirstResponder_(target)
        finally:
            Hit.tabbing = False

    def show_pane(self, pane, resize=True):
        self.end_edit()
        self.pane = pane
        self._clear_hover()
        for name, view in self.panes.items():
            view.setHidden_(name != pane)
        glass = pane in ("signin", "code")
        self.bg.glass = glass
        self._paint_bg()
        self.glass.setHidden_(not glass)
        self.glass.setFrame_(AK.NSMakeRect(0, ARROW_H, POPOVER_W, BODY_H[pane]))
        self.bg.setNeedsDisplay_(True)
        if resize:
            frame = self.panel.frame()
            new_h = ARROW_H + BODY_H[pane]
            top = frame.origin.y + frame.size.height
            self.panel.setFrame_display_(
                AK.NSMakeRect(frame.origin.x, top - new_h, POPOVER_W, new_h), True
            )
        self.bg.setFrame_(AK.NSMakeRect(0, 0, POPOVER_W, ARROW_H + BODY_H[pane]))
        if pane == "settings":
            self._refresh_updated_label()
            self._render_schedule()  # its row says what the schedule wants right now
        if pane == "schedule" and self.schedule_day != datetime.now().weekday():
            self.show_day(datetime.now().weekday())
        self.bg.setNeedsDisplay_(True)
        self.panel.invalidateShadow()
        if self.visible and pane in ("signin", "code"):
            self._focus_pane_field()

    def toggle(self, button):
        if self.visible:
            self.hide()
        elif time.time() - self.closed_at > 0.25:
            self.show(button)

    def show(self, button, pane=None):
        self.show_pane(pane or self.app.opening_pane(), resize=False)
        win = button.window()
        frame = win.frame()
        cx = frame.origin.x + frame.size.width / 2.0
        top = frame.origin.y - 4.0
        screen = win.screen() or AK.NSScreen.mainScreen()
        vis = screen.visibleFrame()
        left = cx - 237.0
        left = max(vis.origin.x + 8.0, min(left, vis.origin.x + vis.size.width - POPOVER_W - 8.0))
        self.bg.arrow_x = max(24.0, min(cx - left, POPOVER_W - 24.0))
        height = ARROW_H + BODY_H[self.pane]
        self.panel.setFrame_display_(AK.NSMakeRect(left, top - height, POPOVER_W, height), True)
        self.bg.setFrame_(AK.NSMakeRect(0, 0, POPOVER_W, height))
        self.bg.setNeedsDisplay_(True)
        self.visible = True
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.orderFrontRegardless()
        self.panel.invalidateShadow()
        mask = AK.NSEventMaskLeftMouseDown | AK.NSEventMaskRightMouseDown | AK.NSEventMaskOtherMouseDown
        self.monitor = AK.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            mask, lambda event: self.hide()
        )
        if self.pane in ("signin", "code"):
            self._focus_pane_field()
        self.app.refresh()

    def hide(self):
        if not self.visible:
            return
        self.visible = False
        self.closed_at = time.time()
        if self.monitor is not None:
            AK.NSEvent.removeMonitor_(self.monitor)
            self.monitor = None
        self.end_edit()
        self.panel.orderOut_(None)
        self._clear_hover()

    # ------------------------------------------------------------- rendering state
    def set_update(self, title, detail, auto, notes=None):
        """The Updates row: "Updates" / "Up to date", or "Update to 1.7" / "Install and restart"."""
        set_label(self.update_title, [(title, 12, "regular", TEXT)])
        set_label(self.update_label, [(detail, 10, "regular", TEXT_3)], "right")
        self.update_row.setToolTip_(notes or None)
        self.update_row.ax_label = f"{title}, {detail}" if detail else title
        self.auto_check.setHidden_(not auto)
        self.auto_row.ax_selected = auto

    def _refresh_updated_label(self):
        ts = self.app.last_update
        if ts is None:
            text = ""
        else:
            secs = int(time.time() - ts)
            if secs < 15:
                text = "Updated just now"
            elif secs < 90:
                text = f"Updated {secs}s ago"
            else:
                text = f"Updated {secs // 60}m ago"
        set_label(self.updated_label, [(text, 10, "mono-regular", TEXT_3)], "right")

    def render(self, st, message=None, dummy=False):
        """st: dict from the app; message: optional status line override."""
        self.state = st
        if st is None:
            self._set_status([(message or "", 11, "regular", TEXT_2)])
            return

        off = st["mode"] == "OFF"
        boosting = st["boost"]
        heating = st["heating"]
        amber = (heating and not off) or boosting
        mode_name = "Scheduled" if st["mode"] == "SCHEDULE" else st["mode"].title()
        target = st["target"]
        heat = heat_hex(target)  # amber, shading to red for high targets
        hot = amber and not dummy and not message  # the pane turns orange while heating
        self._apply_theme(hot, target)
        c = self.c

        # status line
        if message:
            status, colour = message, c["fg2"]
        elif boosting:
            status, colour = "Heating · 1-hour boost", heat
        elif off:
            status, colour = "Heating off", c["fg2"]
        elif heating and st.get("next_change") and st["mode"] == "SCHEDULE":
            status, colour = f"Heating · {mode_name} until {when_text(st['next_change'], clock_only=True)}", heat
        elif heating:
            status, colour = f"Heating · {mode_name}", heat
        elif st.get("next_change") and st["mode"] == "SCHEDULE":
            due = st["next_change"] - time.time() < 60
            when = "now" if due else when_text(st["next_change"], clock_only=True)
            status, colour = f"Idle · {mode_name} {when}", c["fg2"]  # "Idle · Scheduled tomorrow 05:45"
        else:
            status, colour = f"Idle · {mode_name}", c["fg2"]
        if hot and status.startswith("Heating"):
            # the user's choice from Figma: "Heating" in white, the rest dark. White this small is
            # about 3.4:1 on the orange, under 4.5:1; kept on purpose.
            word, rest = status[:7], status[7:]
            self._set_status([(word, 11, "regular", "#ffffff"), (rest, 11, "regular", c["fg2"])])
        else:
            if hot:
                colour = c["fg"]
            self._set_status([(status, 11, "regular", colour)])

        # numeral, scale and columns
        self.scale.set(None if off else target, c["fg3"], c["mark"])
        if off:
            self._set_number("Off", c["numeral"], unit=False, max_size=OFF_PT)
        else:
            num = f"{int(target)}" if float(target).is_integer() else f"{target:.1f}"
            self._set_number(num, c["numeral"])
        set_label(self.current_label, [(f"{st['current']:.1f}°C", 11, "regular", c["numeral"])], "right")
        set_plain(self.current_label, f"Current {st['current']:.1f}°C")
        self.mode_name = mode_name
        self._render_weather()
        water = st.get("hot_water")
        self.water_key.setHidden_(water is None)
        self.water_label.setHidden_(water is None)
        if water is not None:
            text = {"ON": "On", "OFF": "Off", "BOOST": "Boost"}.get(water, water.title())
            set_label(self.water_label, [(text, 11, "regular", c["numeral"])], "right")
            set_plain(self.water_label, f"Hot water {text.lower()}")

        # +/- availability
        for btn in (self.minus_btn, self.plus_btn, self.set_btn):
            btn.enabled = not off
            btn.setAlphaValue_(0.4 if off else 1.0)
        for icon in (self.minus_icon, self.plus_icon):
            icon.setAlphaValue_(1.0)

        # boost button, countdown, note
        self._render_boost(st)

        if not dummy:
            self.set_schedule(st.get("schedule"))
        self.outside_group.setHidden_(dummy or self.weather is None)

        # settings pane checkmarks
        for key, (row, row_icon, icon, name_label, name, check) in self.mode_rows.items():
            selected = key == st["mode"]
            row.fill = rgb(SURFACE) if selected else None
            row.setNeedsDisplay_(True)
            check.setHidden_(not selected)
            row.ax_selected = selected
            set_icon(row_icon, icon, TEXT if selected else TEXT_2, 16)
            set_label(name_label, [(name, 12, "medium" if selected else "regular", TEXT)])
        if self.pane == "settings":
            self._refresh_updated_label()
        self._set_dummy(dummy, off)

    # ------------------------------------------------------------- status banner
    MARQUEE_SPEED = 30.0  # pt per second
    MARQUEE_PAUSE = 1.5   # seconds at each end

    def _set_status(self, parts):
        """Set the status line. Too long to fit, it scrolls to its end and back, like a banner.

        With Reduce Motion on it stays still, and the whole text is in its tooltip.
        """
        set_label(self.status_label, parts)
        text_w = text_width(parts) + 4
        clip_w = self.status_clip.frame().size.width
        self.status_label.setFrame_(AK.NSMakeRect(0, 0, max(text_w, clip_w), 14))
        overflow = max(0.0, text_w - clip_w)
        changed = abs(overflow - self.marquee_overflow) > 0.5
        self.marquee_overflow = overflow
        self.status_clip.setToolTip_("".join(p[0] for p in parts) if overflow else None)
        if changed:
            self.marquee_t0 = time.time()
        moving = overflow > 0 and not reduce_motion()
        if moving and self.marquee_timer is None:
            handler = ClickHandler.alloc().init()
            handler.callback = self._marquee_step
            self._handlers.append(handler)
            self.marquee_timer = AK.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1 / 30.0, handler, "clicked:", None, True)
        elif not moving and self.marquee_timer is not None:
            self.marquee_timer.invalidate()
            self.marquee_timer = None

    def _marquee_step(self):
        if not self.visible:
            self.marquee_t0 = time.time()  # start from the beginning when it opens again
            return
        travel = self.marquee_overflow / self.MARQUEE_SPEED
        elapsed = time.time() - self.marquee_t0
        if elapsed > travel + 2 * self.MARQUEE_PAUSE:
            self.marquee_t0 = time.time()
            elapsed = 0.0
        x = -min(self.marquee_overflow, max(0.0, elapsed - self.MARQUEE_PAUSE) * self.MARQUEE_SPEED)
        self.status_label.setFrameOrigin_((round(x * 2) / 2, 0))

    def _set_number(self, text, colour, unit=True, max_size=NUMERAL_PT):
        """The big numeral, left aligned, its ° raised to the top of the digits.

        Sized down from NUMERAL_PT until the number and ° fit NUMERAL_W ("21.5" is smaller than "21").
        """
        size = max_size
        while True:
            parts = [(text, size, "heavy-display", colour, (-0.08 if unit else -0.05) * size)]
            deg = [("°", size * DEGREE_RATIO, "heavy-display", colour)]
            num_w = text_width(parts)
            deg_w = text_width(deg) if unit else 0
            if num_w + deg_w <= NUMERAL_W or size <= 48:
                break
            size -= 4
        f = font(size, "heavy-display")
        top = 8 - (f.ascender() - f.capHeight())  # digit tops 8pt into the group
        line_h = f.ascender() - f.descender() + 4
        set_label(self.target_label, parts)
        self.target_label.setFrame_(AK.NSMakeRect(-2, top, num_w + 8, line_h))
        set_plain(self.target_label, "Heating off" if not unit else f"Target {text}°")
        self.degree_label.setHidden_(not unit)
        if unit:
            df = font(size * DEGREE_RATIO, "heavy-display")
            dtop = 8 - (df.ascender() - df.capHeight())
            set_label(self.degree_label, deg)
            self.degree_label.setFrame_(AK.NSMakeRect(num_w + 2, dtop, deg_w + 6,
                                                      df.ascender() - df.descender() + 4))
        if self.editing:
            self._place_edit_degree()

    def set_history(self, points, targets, error=None):
        """Fill the last-24-hours chart. points: [(timestamp, temperature)]."""
        if error and self.history_chart.points:
            return  # keep the chart we have; the next try may work
        self._history_args = (points, targets, error)  # redrawn in the other colours by _apply_theme
        end = time.time()
        start = end - HISTORY_HOURS * 3600
        points = [p for p in points if p[0] >= start]
        targets = [p for p in targets if p[0] >= start]
        self.history_chart.set_data(points, targets, start, end)
        if not points:
            set_label(self.history_note, [(error or "No readings yet", 10, "regular", self.c["fg3"])], "center")
            self.history_note.setHidden_(False)
            set_label(self.history_summary, [("", 10, "regular", self.c["fg3"])])
        else:
            self.history_note.setHidden_(True)
            temps = [v for _, v in points]
            set_label(self.history_summary, [
                (" • Low ", 10, "regular", self.c["fg3"]), (f"{min(temps):.1f}°", 10, "regular", self.c["numeral"]),
                (" • High ", 10, "regular", self.c["fg3"]), (f"{max(temps):.1f}°", 10, "regular", self.c["numeral"]),
            ])
        self.history_marks = (start, end)
        self._render_history_hours()

    def _render_history_hours(self):
        """Hour marks every 6 hours on the clock, and "Now" at the right."""
        if not getattr(self, "history_marks", None):
            return
        start, end = self.history_marks
        plot_w = CONTENT_W - 26
        first = datetime.fromtimestamp(start).replace(minute=0, second=0, microsecond=0)
        first += timedelta(hours=(6 - first.hour % 6) % 6 or 6)
        marks = []
        mark = first
        while mark.timestamp() < end - 3 * 3600:  # keep clear of Now
            marks.append((mark.timestamp(), mark.strftime("%H:%M")))
            mark += timedelta(hours=6)
        marks.append((end, "Now"))
        for i, label in enumerate(self.history_hours):
            if i >= len(marks):
                label.setHidden_(True)
                continue
            ts, text = marks[i]
            parts = [(text, 9, "regular", self.c["fg3"])]
            w = math.ceil(text_width(parts)) + 6  # NSTextField insets its text a little
            x = (ts - start) / (end - start) * plot_w - w / 2  # centred on its time...
            x = min(max(x, -3), plot_w - w + 3)                # ...but never past the chart's edges
            if text == "Now":
                x = plot_w - w + 3                             # text flush with the right end of the line
            label.setFrame_(AK.NSMakeRect(16 + x, HISTORY_HOURS_Y, w, 11))
            set_label(label, parts, "center")
            label.setHidden_(False)

    def _render_boost(self, st):
        c = self.c
        boosting = st["boost"]
        if boosting:
            label, icon = "Cancel boost", "timer-off"
        else:
            label, icon = "Boost for 1 hour", "timer"
        set_label(self.boost_text, [(label, 11, "regular", c["fg"])])
        set_icon(self.boost_icon, icon, c["fg"], 15)

        # note under the button
        if boosting and st.get("boost_end"):
            resume = datetime.fromtimestamp(st["boost_end"]).strftime("%H:%M")
            prior = "Schedule" if st["mode"] == "SCHEDULE" else st["mode"].title()
            note = f"{prior} resumes at {resume}"
        elif st["mode"] == "SCHEDULE":
            note = "Following your thermostat schedule"
        elif st["mode"] == "MANUAL":
            note = "Holding a set temperature"
        else:
            note = "Heating is off"
        note_colour = c["numeral"] if self.hot else c["fg"]  # white on orange, as drawn in Figma
        plain_note = True
        if st["mode"] != "OFF" and st["target"] >= VERY_HIGH_TEMP:
            note, note_colour, plain_note = "Very high. Not advised for babies or older people.", RED, False
        elif st["mode"] != "OFF" and st["target"] >= WARN_TEMP:
            note, note_colour, plain_note = "High setting. Each degree costs about 10% more to heat.", heat_hex(st["target"]), False
        if self.editing and self.edit_note:
            note, note_colour = self.edit_note
            plain_note = False
        if self.hot and not plain_note:
            note_colour = c["fg"]  # warnings and typing hints stay black on orange
        self._set_note([(note, 10, "regular", note_colour)])
        self.tick()

    def tick(self):
        """Once a second: refresh the boost countdown."""
        st = self.state
        c = self.c
        if st and st["boost"] and st.get("boost_end"):
            left = max(0, int(st["boost_end"] - time.time()))
            key, text = "Boost", f"{left // 60:02d}:{left % 60:02d} left"
        else:
            key, text = "Mode", getattr(self, "mode_name", "")
        set_label(self.mode_key, [(key, 11, "regular", c["fg3"])])
        set_label(self.countdown_label, [(text, 11, "regular", c["numeral"])], "right")

    # ------------------------------------------------------------- sign-in panes
    def _text_field(self, x, w, secure, placeholder, on_return):
        cls = AK.NSSecureTextField if secure else AK.NSTextField
        tf = cls.alloc().initWithFrame_(AK.NSMakeRect(x, 9.5, w, 17))
        tf.setBezeled_(False)
        tf.setBordered_(False)
        tf.setDrawsBackground_(False)
        tf.setEditable_(True)
        tf.setSelectable_(True)
        tf.setFocusRingType_(AK.NSFocusRingTypeNone)
        tf.setFont_(font(12))
        tf.setTextColor_(rgb(TEXT))
        try:
            tf.cell().setScrollable_(True)
            tf.cell().setWraps_(False)
            tf.setPlaceholderAttributedString_(attributed([(placeholder, 12, "regular", TEXT_3)]))
        except Exception:  # noqa: BLE001
            pass
        if on_return is not None:
            handler = ClickHandler.alloc().init()
            handler.callback = on_return
            self._handlers.append(handler)
            tf.setTarget_(handler)
            tf.setAction_("clicked:")
        return tf

    def _field(self, parent, y, label, placeholder, secure=False, on_return=None, right_pad=0):
        """Label at y with a 36pt translucent input below it. Returns the text field."""
        parent.addSubview_(make_label([(label, 11, "medium", TEXT_2)], 20, y, 280 + GROW, 13))
        box = FieldBox.alloc().initWithFrame_(AK.NSMakeRect(20, y + 19.5, 280 + GROW, 36))
        box.glass = True
        tf = self._text_field(10, 260 + GROW - right_pad, secure, placeholder, on_return)
        box.field = tf
        box.addSubview_(tf)
        parent.addSubview_(box)
        return tf

    def _primary_button(self, parent, y, text, callback):
        btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(20, y, 280 + GROW, 36))
        btn.configure(8, "#e8e7e4", "#f6f5f3")
        btn.focus_hex = "#25282c"  # dark ring on the light button
        btn.callback = callback
        label = make_label([(text, 12, "semibold", "#25282c")], 0, 10.5, 280 + GROW, 15, "center")
        btn.addSubview_(label)
        parent.addSubview_(btn)
        return btn, label

    def _quit_button(self):
        """Top right of the sign-in panes, so you can quit before signing in."""
        btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(273 + GROW, 19, 28, 28))
        btn.configure(6, None, SURFACE)
        btn.callback = lambda: AK.NSApp.terminate_(None)
        btn.ax_label = f"Quit {APP_NAME}"
        btn.addSubview_(icon_view("power", TEXT_2, 16, 6, 6))
        btn.setToolTip_(f"Quit {APP_NAME} (⌘Q)")
        return btn

    def _build_signin(self):
        s = self.signin
        # product row: symbol and name on the left, quit on the right
        symbol = Hit.alloc().initWithFrame_(AK.NSMakeRect(20, 20, 28, 28))
        symbol.configure(8, "#2d3036", None, "#3d4045", 1.0)
        symbol.addSubview_(icon_view("thermometer", TEXT, 18, 5, 5))
        s.addSubview_(symbol)
        s.addSubview_(make_label([(APP_NAME, 14, "semibold", TEXT)], 56, 25.5, 200, 17))
        s.addSubview_(self._quit_button())
        sep = Box.alloc().initWithFrame_(AK.NSMakeRect(20, 68, 280 + GROW, 1))
        sep.colour = rgb("#ffffff", 0.08)
        s.addSubview_(sep)

        s.addSubview_(make_label([("Sign in", 20, "medium", TEXT)], 20, 89, 280 + GROW, 24))
        self.signin_msg = make_label([(SIGNIN_HINT, 11, "regular", TEXT_2)], 20, 119, 280 + GROW, 30, wrap=True)
        s.addSubview_(self.signin_msg)

        self.email_field = self._field(
            s, 155.5, "Email", "name@example.com",
            on_return=lambda: self.panel.makeFirstResponder_(self._password_target()),
        )
        self.password_field = self._field(
            s, 223, "Password", "Password", secure=True,
            on_return=self._submit_signin, right_pad=30,
        )
        box = self.password_field.superview()
        self.password_plain = self._text_field(10, 230 + GROW, False, "Password", self._submit_signin)  # same width as the hidden one
        self.password_plain.setHidden_(True)
        box.addSubview_(self.password_plain)
        self.password_visible = False
        eye = Hit.alloc().initWithFrame_(AK.NSMakeRect(248 + GROW, 6, 24, 24))
        eye.configure(6, None, "#2f3237")
        eye.callback = self._toggle_password
        eye.ax_label = "Show password"
        self.eye_btn = eye
        self.eye_icon = icon_view("eye", TEXT_2, 15, 4.5, 4.5)
        eye.addSubview_(self.eye_icon)
        box.addSubview_(eye)
        self.email_field.setNextKeyView_(self.password_field)
        self.password_field.setNextKeyView_(self.email_field)

        self.signin_btn, self.signin_btn_label = self._primary_button(
            s, 298, "Sign in", self._submit_signin
        )
        forgot = Hit.alloc().initWithFrame_(AK.NSMakeRect(90 + GROW / 2, 342, 140, 20))
        forgot.callback = self._open_forgot
        forgot.addSubview_(make_label([("Forgot password?", 11, "regular", TEXT_2)], 0, 3, 140, 14, "center"))
        s.addSubview_(forgot)

    def _password_target(self):
        return self.password_plain if self.password_visible else self.password_field

    def _password_value(self):
        return str(self._password_target().stringValue())

    def _set_password(self, value):
        self.password_field.setStringValue_(value)
        self.password_plain.setStringValue_(value)

    def _toggle_password(self):
        value = self._password_value()
        self.password_visible = not self.password_visible
        self._set_password(value)
        self.password_field.setHidden_(self.password_visible)
        self.password_plain.setHidden_(not self.password_visible)
        self.password_field.superview().field = self._password_target()
        set_icon(self.eye_icon, "eye-off" if self.password_visible else "eye", TEXT_2, 15)
        self.eye_btn.ax_label = "Hide password" if self.password_visible else "Show password"
        self.panel.makeFirstResponder_(self._password_target())

    def _open_forgot(self):
        try:
            AK.NSWorkspace.sharedWorkspace().openURL_(AK.NSURL.URLWithString_(FORGOT_URL))
        except Exception:  # noqa: BLE001
            pass
        self.hide()

    def _build_code(self):
        s = self.code
        back = Hit.alloc().initWithFrame_(AK.NSMakeRect(20, 20, 24, 28))
        back.configure(6, None, "#2f3237")
        back.callback = self._cancel_code
        back.ax_label = "Back"
        back.addSubview_(icon_view("chevron-left", TEXT, 16, 4, 6))
        s.addSubview_(back)
        s.addSubview_(make_label([("Verification", 14, "semibold", TEXT)], 54, 25.5, 200, 17))
        s.addSubview_(self._quit_button())
        sep = Box.alloc().initWithFrame_(AK.NSMakeRect(20, 68, 280 + GROW, 1))
        sep.colour = rgb("#ffffff", 0.08)
        s.addSubview_(sep)
        self.code_field = self._field(
            s, 88, "Verification code", "6-digit code", on_return=self._submit_code
        )
        self.code_msg = make_label(
            [("Enter the code Hive texted you.", 11, "regular", TEXT_2)], 20, 154, 280 + GROW, 30, wrap=True
        )
        s.addSubview_(self.code_msg)
        self.code_btn, self.code_btn_label = self._primary_button(
            s, 192, "Verify", self._submit_code
        )

    def _set_msg(self, label, text, error=False):
        set_label(label, [(text, 11, "regular", ERROR if error else TEXT_2)], wrap=True)

    def _set_signin_msg(self, text="", error=False):
        self._set_msg(self.signin_msg, text or SIGNIN_HINT, error and bool(text))

    def _set_busy(self, btn, label, text, busy):
        btn.enabled = not busy
        btn.setAlphaValue_(0.55 if busy else 1.0)
        set_label(label, [(text, 12, "semibold", "#25282c")], "center")

    def _reset_buttons(self):
        self._set_busy(self.signin_btn, self.signin_btn_label, "Sign in", False)
        self._set_busy(self.code_btn, self.code_btn_label, "Verify", False)

    def set_login_busy(self, busy, text=""):
        if self.pane == "code":
            self._set_busy(self.code_btn, self.code_btn_label, "Verifying…" if busy else "Verify", busy)
            self._set_msg(self.code_msg, text or "Enter the code Hive texted you.")
        else:
            self._set_busy(
                self.signin_btn, self.signin_btn_label, "Signing in…" if busy else "Sign in", busy
            )
            self._set_signin_msg(text)

    def login_failed(self, text):
        self._reset_buttons()
        if not str(self.email_field.stringValue()):
            self.email_field.setStringValue_(kc_get("username") or "")
        self._set_signin_msg(text, error=True)
        self.app.open_popover("signin")

    def login_done(self):
        self._set_password("")
        self.code_field.setStringValue_("")
        self._set_signin_msg("")
        self._reset_buttons()
        if self.visible and self.pane in ("signin", "code"):
            self.show_pane("control")

    def request_code(self):
        self.code_field.setStringValue_("")
        self._reset_buttons()
        self._set_msg(self.code_msg, "Enter the code Hive texted you.")
        self.app.open_popover("code")

    def show_signed_out(self):
        self.email_field.setStringValue_("")
        self._set_password("")
        self._set_signin_msg("")
        self._reset_buttons()
        self.app.open_popover("signin")

    def _submit_signin(self):
        email = str(self.email_field.stringValue()).strip()
        password = self._password_value()
        if not email or not password:
            self._set_signin_msg("Enter your email and password.", error=True)
            return
        self.app.start_login(email, password)

    def _submit_code(self):
        code = str(self.code_field.stringValue()).strip()
        if not code:
            self._set_msg(self.code_msg, "Enter the code from your text message.", error=True)
            return
        self._set_busy(self.code_btn, self.code_btn_label, "Verifying…", True)
        self._set_msg(self.code_msg, "Checking your code…")
        self.app.submit_code(code)

    def _cancel_code(self):
        self.app.cancel_code()
        self.show_pane("signin")

    def _focus_pane_field(self):
        try:
            AK.NSApp.activateIgnoringOtherApps_(True)
        except Exception:  # noqa: BLE001
            pass
        if self.pane == "code":
            target = self.code_field
        elif str(self.email_field.stringValue()):
            target = self._password_target()
        else:
            target = self.email_field
        self.panel.makeFirstResponder_(target)

    def _set_dummy(self, dummy, off):
        """Placeholder data (before sign-in) is shown dimmed and can't be clicked."""
        self.control.setAlphaValue_(0.4 if dummy else 1.0)
        for btn in (self.settings_btn, self.boost_btn):
            btn.enabled = not dummy
        for btn in (self.minus_btn, self.plus_btn, self.set_btn):
            btn.enabled = not (dummy or off)
        if self.editing and (dummy or off):
            self.end_edit()


# ---------------------------------------------------------------- the app
class LoginCancelled(Exception):
    pass


def keep_auth_errors(auth):
    """pyhiveapi drops some of Hive's sign-in errors and carries on with None.

    Wrap its two sign-in calls so the last error is kept on auth.last_error.
    """
    auth.last_error = None
    client = auth.client
    for name in ("initiate_auth", "respond_to_auth_challenge"):
        call = getattr(client, name)

        def kept(*args, _call=call, **kwargs):
            # pyhiveapi's token refresh wraps AuthParameters in a tuple when no device
            # key is saved, which Hive rejects, so every refresh fails after the first hour.
            if isinstance(kwargs.get("AuthParameters"), tuple):
                kwargs["AuthParameters"] = kwargs["AuthParameters"][0]
            try:
                return _call(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                auth.last_error = exc
                raise

        setattr(client, name, kept)


def friendly_error(exc):
    name = type(exc).__name__
    text = str(exc) or name
    code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")
    if code in ("TooManyRequestsException", "LimitExceededException", "TooManyFailedAttemptsException") \
            or "attempts exceeded" in text.lower():
        return "Hive has blocked sign-in for a while after too many tries. Wait 15 minutes and try again."
    if code == "ResourceNotFoundException" and "client" in text.lower():
        return "Hive has changed its sign-in settings. Ember needs an update (HIVE_LOGIN_INFO)."
    if code == "NotAuthorizedException":
        return "Hive didn't accept that sign-in. Check your email and password."
    if name in ("HiveInvalidPassword", "HiveInvalidUsername"):
        return "Wrong email or password."
    if name == "HiveInvalid2FACode":
        return "That code didn't work. Please sign in again."
    if name in ("HiveReauthRequired", "HiveInvalidDeviceAuthentication"):
        return "Hive needs you to sign in again."
    low = text.lower()
    if "timed out" in low or "timeout" in name.lower() or "connection" in low:
        return "Couldn't reach Hive. Check your connection and try again."
    return text if len(text) < 90 else text[:87] + "…"


def is_session_expired(exc):
    """Hive no longer accepts the session's refresh token (it happens about once a day).

    pyhiveapi would sign in again for its own expiry errors, but Hive's "Invalid Refresh Token"
    comes through as botocore's NotAuthorizedException, so every refresh failed until a restart.
    """
    name = type(exc).__name__
    code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")
    text = str(exc).lower()
    return (code == "NotAuthorizedException" or name in ("HiveReauthRequired", "HiveRefreshTokenExpired")
            or "refresh token" in text)


# ---------------------------------------------------------------- updates
def version_tuple(text):
    """ "v1.7.1" -> (1, 7, 1). Anything unreadable counts as 0."""
    return tuple(int(p) if p.isdigit() else 0 for p in str(text).lstrip("vV").split("."))


def parse_release(payload):
    """(version, zip URL, notes) from GitHub's latest-release reply, or None without a zip."""
    version = str(payload.get("tag_name") or "").lstrip("vV")
    for asset in payload.get("assets") or []:
        if str(asset.get("name", "")).endswith(".zip"):
            return version, asset["browser_download_url"], str(payload.get("body") or "").strip()
    return None


def running_bundle():
    """Path of the running Ember.app, or None when run from source (bash run.sh)."""
    path = str(AK.NSBundle.mainBundle().bundlePath())
    return path if path.endswith(".app") else None


def bundle_signer(path):
    """(team id, bundle id) of a signed app, or None. Updates must match the running app's."""
    import subprocess
    try:
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", path],
                       check=True, capture_output=True, timeout=120)
        info = subprocess.run(["/usr/bin/codesign", "-dv", "--verbose=2", path],
                              capture_output=True, text=True, timeout=60).stderr
    except Exception:  # noqa: BLE001
        return None
    team = re.search(r"^TeamIdentifier=(\S+)", info, re.M)
    ident = re.search(r"^Identifier=(\S+)", info, re.M)
    if not team or team.group(1) == "not" or not ident:
        return None
    return team.group(1), ident.group(1)


# Runs after Ember quits: swap the app in place, put the old one back if that fails, open it.
SWAP_SCRIPT = """
pid="$1"; new="$2"; dest="$3"
while kill -0 "$pid" 2>/dev/null; do sleep 0.2; done
rm -rf "$dest.old"
if mv "$dest" "$dest.old" && /usr/bin/ditto "$new" "$dest"; then
  rm -rf "$dest.old"
else
  rm -rf "$dest"; mv "$dest.old" "$dest"
fi
open "$dest"
"""


class ThermostatOffline(Exception):
    """Hive answered, but had no temperature from the thermostat (it or the hub lost its connection)."""


def short_error(exc, where="refresh"):
    """A few words for the 240pt status line. The full error goes to LOG_PATH."""
    name = type(exc).__name__
    low = str(exc).lower()
    if isinstance(exc, ThermostatOffline):
        return "Thermostat not responding. Retrying in a minute"
    if name in ("HiveReauthRequired", "HiveInvalidDeviceAuthentication") or "unauthor" in low:
        return "Hive wants you to sign in again"
    if "timed out" in low or "timeout" in name.lower() or "connection" in low:
        return "Couldn't reach Hive. Retrying in a minute"
    if where == "refresh":  # refreshes repeat every REFRESH_SECONDS
        return "Hive isn't responding. Retrying in a minute"
    return "Hive couldn't make that change. Try again"


def boiler_working(session, device, h):
    """True while Hive says the boiler is heating, as the Hive app shows it ("Heating to 18°").

    That is the thermostat's props.working. pyhiveapi's getState only compares the room with the
    target, which disagrees with Hive near the target (Hive keeps heating a little past it).
    """
    try:
        working = session.data.products[device["hiveID"]]["props"]["working"]
        if isinstance(working, bool):
            return working
    except Exception:  # noqa: BLE001
        pass
    return h.getState(device) == "ON"


def hot_water_state(session):
    """"ON" or "OFF" for the account's hot water, "BOOST" while boosted, or None without a tank."""
    try:
        tanks = session.deviceList.get("water_heater") or []
        if not tanks:
            return None
        hw = session.hotwater
        if hw.getBoost(tanks[0]) == "ON":
            return "BOOST"
        state = hw.getState(tanks[0])
        return "ON" if str(state).upper() == "ON" else "OFF"
    except Exception:  # noqa: BLE001
        return None


class HiveApp(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, title=APP_NAME, quit_button=None)
        self.session = None
        self.device = None
        self.zones = []                # every thermostat on the account
        self.pending_device = None     # the thermostat a waiting tap is for
        self.lock = threading.Lock()
        self.nudge_lock = threading.Lock()
        self.last_target = None
        self.pending_target = None
        self._send_timer = None
        self._init_sending()
        self.last_update = None
        self.refreshing = False
        self.offline_logged = False  # the "thermostat not responding" error is logged once per outage
        self.refresh_again = False     # a refresh was asked for while one was running
        self.action_seq = 0            # bumped by each change shown before Hive confirms it
        self.connecting = False
        self.login_source = None       # "saved" at start up, "ui" when typed in
        self.awaiting_code = False
        self.code_event = None
        self.code_value = None
        self.state = None
        self.boost_end = None
        self.history_at = 0.0          # when the last-24-hours chart last asked Hive
        self.history_busy = False
        self.weather_at = 0.0          # when Hive's weather service was last asked
        self.reauth_at = 0.0           # when Ember last signed in again by itself (see _reauth)
        self.weather_busy = False
        self.update_at = 0.0           # when GitHub was last asked for a newer release
        self.update_busy = False
        self.update = None             # (version, url, notes) of a newer release
        self.update_state = ("Updates", "")
        self.ui = None
        self.handler = None
        self.button = None

        self.timer = rumps.Timer(lambda _: (self.refresh(), self._maybe_check_update()), REFRESH_SECONDS)
        self.timer.start()
        self.clock = rumps.Timer(lambda _: self._tick(), 1)
        self.clock.start()
        # AppKit work has to start on the main thread once the app is running.
        self._start = rumps.Timer(self._first_run, 1)
        self._start.start()

    # ---------- start up
    def _first_run(self, timer):
        timer.stop()
        self._install_popover()
        username, password = kc_get("username"), kc_get("password")
        if username and password:
            self._show_message("Connecting…")
            self.login_source = "saved"
            self._begin_login(username, password)
        else:
            self._show_message("Sign in to see your heating")
            self.open_popover("signin")

    def _install_popover(self):
        try:
            AK.NSApp.setActivationPolicy_(AK.NSApplicationActivationPolicyAccessory)
        except Exception:  # noqa: BLE001
            pass
        try:
            icon_path = os.path.join(dial.assets_dir(), "AppIcon.png")
            image = AK.NSImage.alloc().initWithContentsOfFile_(icon_path)
            if image is not None:
                AK.NSApp.setApplicationIconImage_(image)
        except Exception:  # noqa: BLE001
            pass
        self.ui = PopoverUI(self)
        self._show_update(*self.update_state)
        item = self._nsapp.nsstatusitem
        item.setMenu_(None)  # clicks now go to our popover instead of a menu
        button = item.button()
        self.button = button
        # equal-width digits, so the item doesn't change width and nudge its neighbours
        bar_size = AK.NSFont.menuBarFontOfSize_(0).pointSize()
        button.setFont_(AK.NSFont.monospacedDigitSystemFontOfSize_weight_(bar_size, AK.NSFontWeightRegular))
        self.handler = ClickHandler.alloc().init()
        self.handler.callback = lambda: self.ui.toggle(self.button)
        button.setTarget_(self.handler)
        button.setAction_("clicked:")
        self._set_bar(None)

    def opening_pane(self):
        if self.awaiting_code:
            return "code"
        if self.session is not None:
            return "control"
        if self.connecting and self.login_source == "saved":
            return "control"
        return "signin"

    def open_popover(self, pane):
        if self.button is None or self.ui is None:
            return
        if self.ui.visible:
            self.ui.show_pane(pane)
        else:
            self.ui.show(self.button, pane)

    def _tick(self):
        if self.ui is not None and self.ui.visible:
            self.ui.tick()
            if self.ui.pane == "settings":
                self.ui._refresh_updated_label()

    # ---------- menu bar item
    def _set_bar(self, st):
        button = self.button
        if button is None:
            return
        if st is None:
            amber, title = False, "--°"
        else:
            amber = (st["heating"] and st["mode"] != "OFF") or st["boost"]
            if st["mode"] == "OFF":
                title = "Off"
            else:
                t = st["target"]
                title = f"{int(t)}°" if float(t).is_integer() else f"{t:.1f}°"
        png = dial.icon_png(
            "thermometer", rgb_tuple(heat_hex(st["target"])) if amber else (255, 255, 255)
        )
        img = ns_image(png, 14)
        img.setTemplate_(not amber)  # amber keeps its colour, otherwise follows the menu bar
        button.setImage_(img)
        button.setImagePosition_(AK.NSImageLeft)
        button.setTitle_(title)

    # ---------- login
    def start_login(self, username, password):
        """Called from the sign-in screen."""
        if self.connecting:
            return
        self.ui.set_login_busy(True, "Signing in…")
        self.login_source = "ui"
        self._begin_login(username, password)

    def _begin_login(self, username, password):
        self.connecting = True
        threading.Thread(target=self._login_worker, args=(username, password), daemon=True).start()

    def _login_worker(self, username, password):
        try:
            session = self._login(username, password, True, self._get_code)
            if session is None:
                raise LoginCancelled()
            climate = session.deviceList.get("climate") or []
            if not climate:
                raise RuntimeError("No heating found on this Hive account.")
            kc_set("username", username)
            kc_set("password", password)
            self.session = session
            self.zones = climate
            saved = NSUserDefaults.standardUserDefaults().stringForKey_("zone")
            self.device = next((d for d in climate if d["hiveID"] == saved), climate[0])
            AppHelper.callAfter(self._signed_in)
        except LoginCancelled:
            AppHelper.callAfter(self.ui.login_failed, "Sign in cancelled.")
        except Exception as exc:  # noqa: BLE001
            log_error("sign in")
            AppHelper.callAfter(self.ui.login_failed, friendly_error(exc))
        finally:
            self.connecting = False

    def _signed_in(self):
        self.ui.login_done()
        self._show_zones()
        self.refresh(user=True)

    REAUTH_GAP = 300  # seconds between tries at signing in again

    def _reauth(self):
        """Sign in again with the saved login, quietly, keeping what is on screen meanwhile."""
        if self.connecting or time.time() - self.reauth_at < self.REAUTH_GAP:
            return
        username, password = kc_get("username"), kc_get("password")
        if not (username and password):
            self._show_message("Hive wants you to sign in again")
            return
        self.reauth_at = time.time()
        self.connecting = True
        self.login_source = "saved"
        self._show_message("Signing in to Hive again…")
        threading.Thread(target=self._reauth_worker, args=(username, password), daemon=True).start()

    def _reauth_worker(self, username, password):
        try:
            session = self._login(username, password, True, self._get_code)  # SMS only if Hive asks
            if session is None:
                raise LoginCancelled()
            climate = session.deviceList.get("climate") or []
            if not climate:
                raise RuntimeError("No heating found on this Hive account.")
            with self.lock:
                old = self.device["hiveID"] if self.device else None
                self.session = session
                self.zones = climate
                self.device = next((d for d in climate if d["hiveID"] == old), climate[0])
                with self.nudge_lock:  # a waiting tap still goes to its thermostat
                    if self.pending_device is not None:
                        pid = self.pending_device["hiveID"]
                        self.pending_device = next((d for d in climate if d["hiveID"] == pid), self.device)
            self.reauth_at = 0.0
            AppHelper.callAfter(self._signed_in)
        except LoginCancelled:
            self._show_message("Hive wants you to sign in again")
        except Exception as exc:  # noqa: BLE001
            log_error("sign in again")
            code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")
            if code == "NotAuthorizedException" or type(exc).__name__.startswith("HiveInvalid"):
                AppHelper.callAfter(self.ui.login_failed, friendly_error(exc))  # the saved password no longer works
            else:
                self._show_message("Couldn't reach Hive. Retrying in a few minutes")
        finally:
            self.connecting = False

    def _show_zones(self):
        names = zone_names(self.session, self.zones) if self.session else []
        index = self.zones.index(self.device) if self.device in self.zones else 0
        self.ui.set_zones(names, index)

    def select_zone(self, index):
        """Show and control another thermostat. A tap still waiting goes to the zone it was for."""
        if not self.session or not 0 <= index < len(self.zones):
            return
        device = self.zones[index]
        if device is self.device:
            self.ui.show_pane("settings")
            return
        with self.lock:
            self.device = device
        NSUserDefaults.standardUserDefaults().setObject_forKey_(device["hiveID"], "zone")
        self.action_seq += 1  # a read already running is for the old zone
        self.state = None
        self.last_target = None
        self.boost_end = None
        self.history_at = 0.0
        self._show_zones()
        self.ui.set_history([], [])
        self.ui.render(DUMMY_STATE, "Loading…", True)
        self.ui.show_pane("control")
        self.refresh(user=True)

    def _get_code(self):
        """Runs on the login thread: ask for the SMS code in the popover and wait."""
        self.code_event = threading.Event()
        self.code_value = None
        self.awaiting_code = True
        AppHelper.callAfter(self.ui.request_code)
        self.code_event.wait(300)
        self.awaiting_code = False
        return self.code_value

    def submit_code(self, code):
        self.code_value = code
        if self.code_event is not None:
            self.code_event.set()

    def cancel_code(self):
        self.code_value = None
        if self.code_event is not None:
            self.code_event.set()

    def _login(self, username, password, use_saved_device, get_code):
        """Log in, handling the saved 'remembered device' and SMS 2FA."""
        session = Hive(username=username, password=password)
        saved = json.loads(kc_get("device") or "[]") if use_saved_device else []
        if saved:
            (
                session.auth.device_group_key,
                session.auth.device_key,
                session.auth.device_password,
            ) = saved

        keep_auth_errors(session.auth)
        try:
            result = session.login()
            if result is None:
                raise TypeError("no reply")
        except Exception as exc:  # noqa: BLE001
            hidden = session.auth.last_error
            if hidden is not None and not str(type(exc).__name__).startswith("Hive"):
                # pyhiveapi swallowed Hive's real answer (often "too many attempts").
                # That says nothing about the saved device, so keep it.
                raise hidden from exc
            if saved:
                log_error("sign in with the saved device")
                # Saved device was rejected: forget it and go through SMS again.
                try:
                    keyring.delete_password(SERVICE, "device")
                except keyring.errors.PasswordDeleteError:
                    pass
                return self._login(username, password, False, get_code)
            raise

        if result.get("ChallengeName") == SMS_REQUIRED:
            code = get_code()
            if not code:
                return None
            session.sms2fa(code, result)
            if session.auth.device_key:
                session.auth.device_registration(f"{APP_NAME} menu bar")
                kc_set("device", json.dumps(list(session.auth.get_device_data())))

        config = {
            "username": username,
            "password": password,
            "tokens": dict(session.tokens.tokenData),
        }
        if session.auth.device_key:
            config["device_data"] = list(session.auth.get_device_data())
        session.api.timeout = 30  # library default of 5s is too short for Hive's servers
        last_err = None
        for _ in range(4):
            try:
                session.startSession(config)
                return session
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(3)
        raise last_err

    # ---------- updates (GitHub releases made by release.sh)
    @staticmethod
    def auto_update():
        value = NSUserDefaults.standardUserDefaults().objectForKey_("check_updates")
        return True if value is None else bool(value)

    def toggle_auto_update(self):
        NSUserDefaults.standardUserDefaults().setBool_forKey_(not self.auto_update(), "check_updates")
        self._show_update(*self.update_state)

    def _show_update(self, title, detail):
        """Main thread only."""
        self.update_state = (title, detail)
        if self.ui is not None:
            self.ui.set_update(title, detail, self.auto_update(), self.update[2] if self.update else None)

    def _maybe_check_update(self):
        if self.auto_update() and time.time() - self.update_at > UPDATE_SECONDS:
            self.check_update()

    def update_clicked(self):
        if self.update_busy:
            return
        if self.update is not None:
            self.install_update()
        else:
            self.check_update(user=True)

    def check_update(self, user=False):
        if self.update_busy:
            return
        self.update_busy = True
        self.update_at = time.time()
        if user:
            self._show_update("Updates", "Checking…")
        threading.Thread(target=self._check_update_worker, args=(user,), daemon=True).start()

    def _check_update_worker(self, user):
        try:
            reply = requests.get(UPDATE_URL, headers={"Accept": "application/vnd.github+json"}, timeout=20)
            if reply.status_code == 404:  # no releases yet
                release = None
            else:
                reply.raise_for_status()
                release = parse_release(reply.json())
            if release and version_tuple(release[0]) > version_tuple(VERSION):
                self.update = release
                state = (f"Update to {release[0]}", "Install and restart")
            else:
                self.update = None
                state = ("Updates", f"Up to date · {VERSION}")
        except Exception:  # noqa: BLE001
            log_error("update check")
            state = ("Updates", "Couldn't check. Tap to try again") if user else self.update_state
            self.update_at = time.time() - UPDATE_SECONDS + 3600  # try again in an hour
        finally:
            self.update_busy = False
        AppHelper.callAfter(self._show_update, *state)

    def install_update(self):
        dest = running_bundle()
        if dest is None:
            self._show_update(self.update_state[0], "Only in the built app")
            return
        if not os.access(os.path.dirname(dest), os.W_OK):
            self._show_update(self.update_state[0], "Can't write to that folder")
            return
        self.update_busy = True
        self._show_update(self.update_state[0], "Downloading…")
        threading.Thread(target=self._install_worker, args=(dest,), daemon=True).start()

    def _install_worker(self, dest):
        import shutil
        import subprocess
        try:
            version, url, _ = self.update
            shutil.rmtree(UPDATE_DIR, ignore_errors=True)
            os.makedirs(UPDATE_DIR)
            zip_path = os.path.join(UPDATE_DIR, "Ember.zip")
            with requests.get(url, stream=True, timeout=60) as reply:
                reply.raise_for_status()
                with open(zip_path, "wb") as out:
                    for chunk in reply.iter_content(1 << 20):
                        out.write(chunk)
            AppHelper.callAfter(self._show_update, self.update_state[0], "Checking the download…")
            subprocess.run(["/usr/bin/ditto", "-x", "-k", zip_path, UPDATE_DIR], check=True, timeout=300)
            apps = [n for n in os.listdir(UPDATE_DIR) if n.endswith(".app")]
            if not apps:
                raise RuntimeError("no app in the download")
            new = os.path.join(UPDATE_DIR, apps[0])
            subprocess.run(["/usr/bin/xattr", "-dr", "com.apple.quarantine", new], capture_output=True)
            mine = bundle_signer(dest)
            theirs = bundle_signer(new)
            if mine is None or theirs != mine:  # only ever install an app signed like this one
                raise RuntimeError(f"signature doesn't match (running {mine}, download {theirs})")
            subprocess.Popen(["/bin/sh", "-c", SWAP_SCRIPT, "swap", str(os.getpid()), new, dest],
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            AppHelper.callAfter(AK.NSApp.terminate_, None)
        except Exception:  # noqa: BLE001
            log_error("update install")
            self.update_busy = False
            AppHelper.callAfter(self._show_update, self.update_state[0], "Update failed. Tap to try again")

    def sign_out(self):
        for key in ("username", "password", "device"):
            try:
                keyring.delete_password(SERVICE, key)
            except keyring.errors.PasswordDeleteError:
                pass
        self.session = None
        self.device = None
        self.zones = []
        self.state = None
        self.last_target = None
        self.history_at = 0.0
        self.weather_at = 0.0
        self._set_bar(None)
        self._show_message("Sign in to see your heating")
        self.ui.set_zones([], 0)
        self.ui.show_signed_out()

    def _show_message(self, text):
        if self.ui is None:
            return
        if self.state is None:
            AppHelper.callAfter(self.ui.render, DUMMY_STATE, text, True)
        else:
            AppHelper.callAfter(self.ui.render, self.state, text)

    def _show_error(self, where, exc):
        if isinstance(exc, ThermostatOffline):
            if not self.offline_logged:  # once per outage, not every minute
                log_error(where)
                self.offline_logged = True
        else:
            log_error(where)
        self._show_message(short_error(exc, where))

    # ---------- reading
    def refresh(self, user=False):
        if not self.session:
            return
        if self.refreshing:
            if user:  # run again once this one ends, so a change made meanwhile shows up
                self.refresh_again = True
            return
        self.refreshing = True
        threading.Thread(target=self._refresh_worker, args=(user,), daemon=True).start()

    def _refresh_worker(self, user):
        seq = self.action_seq
        try:
            with self.lock:
                h = self.session.heating
                if user:  # skip the library's rate limit for a manual refresh
                    try:
                        self.session.config.lastUpdate = datetime.now() - timedelta(days=1)
                    except Exception:  # noqa: BLE001
                        pass
                device = self.device
                self.session.updateData(device)
                current = h.getCurrentTemperature(device)
                target = h.getTargetTemperature(device)
                if current is None or target is None:  # Hive has no reading: thermostat or hub offline
                    raise ThermostatOffline(f"current={current!r} target={target!r}")
                current, target = float(current), float(target)
                mode = (h.getMode(device) or "SCHEDULE").upper()
                boost = h.getBoostStatus(device) == "ON"
                heating = boiler_working(self.session, device, h)
                hot_water = hot_water_state(self.session)
                minutes = None
                if boost:
                    try:
                        minutes = int(h.getBoostTime(device))
                    except Exception:  # noqa: BLE001
                        minutes = None
                next_change = None  # when the schedule next turns the heating on, or off if it is on
                schedule = None
                try:
                    product = self.session.data.products[device["hiveID"]]
                    schedule = copy.deepcopy(product["state"]["schedule"])
                    if mode == "SCHEDULE":
                        next_change = next_heat_on(schedule, current, on=not heating)
                except Exception:  # noqa: BLE001
                    next_change = None  # the schedule is a nice-to-have; never fail a refresh on it
            if self.pending_target is not None and self.pending_device is device:  # a tap is waiting; keep it
                target = self.pending_target
            else:
                self.last_target = target
            self.last_update = time.time()
            self._track_boost(boost, minutes)
            st = {
                "current": current,
                "target": target,
                "mode": mode if mode in ("SCHEDULE", "MANUAL", "OFF") else "SCHEDULE",
                "boost": boost,
                "heating": heating and mode != "OFF",
                "boost_end": self.boost_end if boost else None,
                "next_change": next_change,
                "schedule": schedule,
                "hot_water": hot_water,
            }
            if seq != self.action_seq:  # read before the latest change; don't undo it on screen
                self.refresh_again = True
            else:
                AppHelper.callAfter(self._apply_state, st)
            self.offline_logged = False
        except Exception as exc:  # noqa: BLE001
            if is_session_expired(exc):
                log_error("refresh (signing in again)")
                self._reauth()
            else:
                self._show_error("refresh", exc)
        finally:
            self.refreshing = False
            if self.refresh_again:
                self.refresh_again = False
                self.refresh(user=True)

    def _track_boost(self, boost, minutes):
        """Keep a local end time so the countdown can tick every second."""
        if not boost:
            self.boost_end = None
            return
        if minutes is None:
            minutes = BOOST_MINUTES
        expected = time.time() + minutes * 60
        if self.boost_end is None or abs(self.boost_end - expected) > 90:
            self.boost_end = expected - 30  # the API reports whole minutes

    def _apply_state(self, st):
        self.state = st
        self._set_bar(st)
        if self.ui is not None:
            self.ui.render(st)
        if time.time() - self.history_at > HISTORY_SECONDS and not self.history_busy:
            self.history_busy = True
            self.history_at = time.time()
            threading.Thread(target=self._history_worker, daemon=True).start()
        if time.time() - self.weather_at > WEATHER_SECONDS and not self.weather_busy:
            self.weather_busy = True
            self.weather_at = time.time()
            threading.Thread(target=self._weather_worker, daemon=True).start()

    def _weather_worker(self):
        """Outside temperature from Hive's weather service, by the postcode on the Hive account."""
        try:
            with self.lock:
                user = dict(self.session.data.user or {})
            postcode = user.get("postcode") or user.get("postCode")
            if not postcode:
                raise ValueError(f"no postcode on the Hive account (fields: {sorted(user)})")
            country = user.get("countryCode") or user.get("country") or "GB"
            reply = requests.get(WEATHER_URL, params={"postcode": postcode, "country": country}, timeout=20)
            reply.raise_for_status()
            temp, description = parse_weather(reply.json())
            AppHelper.callAfter(self.ui.set_weather, temp, description)
        except Exception:  # noqa: BLE001
            log_error("weather")
            self.weather_at = time.time() - WEATHER_SECONDS + 300  # try again in five minutes
        finally:
            self.weather_busy = False

    def _history_worker(self):
        """Room temperature for the last day, plus what the schedule asked for meanwhile."""
        try:
            with self.lock:
                token = self.session.tokens.tokenData["token"]
                product_id = self.device["hiveID"]
                try:
                    schedule = self.session.data.products[product_id]["state"]["schedule"]
                except Exception:  # noqa: BLE001
                    schedule = None
            end = int(time.time() * 1000)
            start = end - HISTORY_HOURS * 3600 * 1000
            reply = requests.get(
                HISTORY_URL.format(product_id),
                params={"start": start, "end": end, "timeUnit": "MINUTES", "rate": 30, "operation": "AVG"},
                headers={"authorization": token, "content-type": "application/json", "Accept": "*/*"},
                timeout=30,
            )
            reply.raise_for_status()
            points = parse_history(reply.json())
            targets = []
            if schedule and self.state and self.state["mode"] == "SCHEDULE":
                targets = [(ts, schedule_target_at(schedule, ts)) for ts, _ in points]
                targets = [(ts, v) for ts, v in targets if v is not None]
            if self.device and self.device["hiveID"] == product_id:  # not if the zone changed meanwhile
                AppHelper.callAfter(self.ui.set_history, points, targets)
        except Exception:  # noqa: BLE001
            log_error("history")
            self.history_at = time.time() - HISTORY_SECONDS + 120  # try again in a couple of minutes
            AppHelper.callAfter(self.ui.set_history, [], [], "History isn't available right now")
        finally:
            self.history_busy = False

    # ---------- controls
    def _init_sending(self):
        # One send to Hive at a time, always the newest value. Before this, each pause in a run of
        # taps started its own send; they queued on self.lock and could land in any order, so
        # stepping 18, 19, 20, 21 then back to 17 could leave Hive on 20.
        self.send_lock = threading.Lock()
        self.pending_seq = 0  # bumped by every tap; a send only clears the tap it sent

    def _queue_target(self, temp):
        """Show the new target at once, then send it to Hive after a short pause.

        Every tap restarts the pause, so a run of taps becomes one request
        with the final value. That keeps the UI quick and stops Hive's
        servers from rejecting a burst of calls.
        """
        temp = max(MIN_TEMP, min(MAX_TEMP, round(temp * 2) / 2))
        with self.nudge_lock:
            self.pending_target = temp
            self.pending_device = self.device
            self.pending_seq += 1
            self.action_seq += 1  # a refresh already running read Hive before this tap
            self.last_target = temp
            if self._send_timer is not None:
                self._send_timer.cancel()
            self._send_timer = threading.Timer(SEND_DELAY, self._flush_target)
            self._send_timer.daemon = True
            self._send_timer.start()
        if self.state:
            st = dict(self.state)
            st["target"] = temp  # heating stays as Hive last said; the boiler takes a while to react
            self._apply_state(st)  # called on the main thread

    def _flush_target(self):
        with self.send_lock:  # a send still going finishes first; then this one takes the newest value
            with self.nudge_lock:
                temp, device, seq = self.pending_target, self.pending_device, self.pending_seq
            if temp is None:
                return  # an earlier send already took it
            self._send_pending(temp, device)
            with self.nudge_lock:
                if self.pending_seq == seq:  # no tap since; refreshes may show Hive's value again
                    self.pending_target = None
                    self._send_timer = None
        self.refresh(user=True)

    def _send_pending(self, temp, device):
        try:
            ok = self._send_target(temp, device)
            if ok is False:  # Hive turned the change down; try once more
                time.sleep(1.5)
                try:
                    self.session.hiveRefreshTokens(True)  # force a fresh login token
                except Exception:  # noqa: BLE001
                    pass
                ok = self._send_target(temp, device)
            if ok is False:
                self._show_message("Hive didn't accept that change. Try again.")
                time.sleep(2)
        except Exception as exc:  # noqa: BLE001
            if is_session_expired(exc):
                log_error("set temperature (signing in again)")
                self._reauth()
            else:
                self._show_error("set temperature", exc)

    def _send_target(self, temp, device):
        with self.lock:
            return self.session.heating.setTargetTemperature(device, temp)

    def nudge(self, delta):
        if not self.session:
            return
        if self.state and self.state["mode"] == "OFF":
            return
        base = self.pending_target if self.pending_device is self.device else None
        if base is None:
            base = self.state["target"] if self.state else self.last_target
        if base is None:
            return
        self._queue_target(base + delta)

    def set_target(self, temp):
        """A value typed into the dial. The popover has already checked and confirmed it."""
        if not self.session:
            return
        self._queue_target(temp)

    def toggle_boost(self):
        if not self.session or not self.state:
            return
        st = dict(self.state)  # show the change at once; the refresh afterwards corrects it
        self.action_seq += 1
        device = self.device
        if self.state["boost"]:
            st["boost"] = False
            st["boost_end"] = None
            self.boost_end = None
            self._apply_state(st)

            def cancel():
                try:
                    with self.lock:
                        self.session.heating.setBoostOff(device)
                except Exception as exc:  # noqa: BLE001
                    self._show_error("cancel boost", exc)
                    time.sleep(2)  # let the message show before the real state replaces it
                self.refresh(user=True)

            threading.Thread(target=cancel, daemon=True).start()
            return

        target = self.state["target"]
        temp = min(MAX_TEMP, max(MIN_TEMP, min(target + BOOST_OFFSET, max(target, BOOST_CAP))))
        self.boost_end = time.time() + BOOST_MINUTES * 60
        st.update(boost=True, target=temp, heating=True, boost_end=self.boost_end)
        self._apply_state(st)

        def start():
            try:
                with self.lock:
                    self.session.heating.setBoostOn(device, BOOST_MINUTES, temp)
            except Exception as exc:  # noqa: BLE001
                self._show_error("start boost", exc)
                time.sleep(2)  # let the message show before the real state replaces it
            self.refresh(user=True)

        threading.Thread(target=start, daemon=True).start()

    def set_mode(self, mode):
        if not self.session:
            return
        if self.state:  # tick the new mode straight away
            self.action_seq += 1
            st = dict(self.state)
            st["mode"] = mode
            AppHelper.callAfter(self._apply_state, st)
        device = self.device

        def work():
            try:
                with self.lock:
                    self.session.heating.setMode(device, mode)
                self.refresh(user=True)
            except Exception as exc:  # noqa: BLE001
                self._show_error("set mode", exc)

        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    HiveApp().run()
