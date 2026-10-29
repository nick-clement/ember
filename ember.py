#!/usr/bin/env python3
"""Hive heating control in the macOS menu bar.

Uses the unofficial pyhiveapi library (pip package: pyhive-integration).
Login details and the device token are kept in the macOS Keychain.

UI follows the Figma designs: a compact graphite popover with a fine dial,
a scheduled / boost control pane, and a settings and mode pane.
"""
import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta

import AppKit as AK
import keyring
import objc
import requests
import rumps
from Foundation import NSData, NSObject
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

# ---------------------------------------------------------------- design tokens
POPOVER_W = 320
ARROW_H = 8
APP_NAME = "Ember"
BODY_H = {"control": 572, "settings": 419, "signin": 379, "code": 248}
SIGNIN_HINT = "Your home, from your menu bar."
FORGOT_URL = "https://my.hivehome.com/"

BG = "#26282b"
BORDER = "#44464a"
SURFACE = "#313337"
SEPARATOR = "#3a3c40"
TEXT = "#f3f2f0"
TEXT_2 = "#b1b2b6"
TEXT_3 = "#888b91"
AMBER = "#eba76e"
AMBER_SURFACE = "#3d3129"
ERROR = "#e0877b"
FIELD_BG = "#1f2124"
RED = "#e8604e"

# The ° beside the big number, inside its 87pt box. Its top lines up with the digits.
DEGREE_PT = 30
DEGREE_GAP = -4.0
DEGREE_Y = 9.0

# Heat colour: amber up to WARM_TEMP, shading to red by HOT_TEMP.
WARM_TEMP = 23.0
HOT_TEMP = 26.0
WARN_TEMP = 24.0       # show a warning line from here
VERY_HIGH_TEMP = 28.0  # stronger wording from here
CONFIRM_TEMP = 26.0    # typed-in values above this ask first
BOOST_CAP = 25.0       # a boost never aims above this unless the target already does
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
            f.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} {where}\n{traceback.format_exc()}\n")
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
    """weight "mono-regular" etc. gives equal-width digits, for numbers that tick."""
    if weight.startswith("mono-"):
        return AK.NSFont.monospacedDigitSystemFontOfSize_weight_(size, _FONT_WEIGHT[weight[5:]])
    return AK.NSFont.systemFontOfSize_weight_(size, _FONT_WEIGHT[weight])


def attributed(parts, align="left", wrap=False):
    """parts: list of (text, size, weight, hex colour[, kern])."""
    para = AK.NSMutableParagraphStyle.alloc().init()
    para.setAlignment_(_ALIGN[align])
    para.setLineBreakMode_(AK.NSLineBreakByWordWrapping if wrap else AK.NSLineBreakByClipping)
    out = AK.NSMutableAttributedString.alloc().init()
    for part in parts:
        text, size, weight, colour = part[:4]
        kern = part[4] if len(part) > 4 else 0.0
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


def make_label(parts, x, y, w, h, align="left", wrap=False):
    tf = AK.NSTextField.alloc().initWithFrame_(AK.NSMakeRect(x, y, w, h))
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
    tf.setAttributedStringValue_(attributed(parts, align, wrap))
    return tf


def set_label(tf, parts, align="left", wrap=False):
    tf.setAttributedStringValue_(attributed(parts, align, wrap))


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
    """A clickable rounded surface with hover and pressed states."""

    def initWithFrame_(self, frame):
        self = objc.super(Hit, self).initWithFrame_(frame)
        if self is None:
            return None
        self.callback = None
        self.radius = 0.0
        self.fill = None
        self.hover_fill = None
        self.pressed_fill = None
        self.border = None
        self.border_w = 1.0
        self.hovering = False
        self.pressed = False
        self.enabled = True
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
    def configure(self, radius, fill=None, hover=None, border=None, border_w=1.0):
        self.radius = radius
        self.fill = rgb(fill) if fill else None
        self.hover_fill = rgb(hover) if hover else None
        self.pressed_fill = rgb(press_hex(hover)) if hover else None
        self.border = rgb(border) if border else None
        self.border_w = border_w
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        fill = self.fill
        if self.enabled and self.hovering and self.hover_fill is not None:
            fill = self.hover_fill
        if self.enabled and self.pressed and self.pressed_fill is not None:
            fill = self.pressed_fill
        if fill is None and self.border is None:
            return
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

    def hitTest_(self, point):
        local = self.convertPoint_fromView_(point, self.superview())
        return self if AK.NSPointInRect(local, self.bounds()) else None

    def acceptsFirstMouse_(self, event):
        return True

    def mouseEntered_(self, event):
        self.hovering = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        self.hovering = False
        self.pressed = False
        self.setNeedsDisplay_(True)

    def mouseDown_(self, event):
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


class NumberHit(Hit):
    """Invisible click area over the target number, with a text cursor to hint it is editable."""

    def resetCursorRects(self):
        if self.enabled:
            self.addCursorRect_cursor_(self.bounds(), AK.NSCursor.IBeamCursor())


class PopoverBackground(FlippedView):
    """Rounded graphite body with the small anchor arrow towards the menu bar."""

    def initWithFrame_(self, frame):
        self = objc.super(PopoverBackground, self).initWithFrame_(frame)
        self.arrow_x = 237.0
        self.glass = False  # sign-in panes: translucent tint over a blurred backdrop
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
            rgb(BG).setFill()
            path.fill()
            rgb(BORDER).setStroke()
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


class HistoryChart(FlippedView):
    """Room temperature over the last day, with the schedule's target behind it.

    Where the schedule asked for more than the room had, the gap is shaded amber.
    """

    def initWithFrame_(self, frame):
        self = objc.super(HistoryChart, self).initWithFrame_(frame)
        self.points = []     # [(timestamp, room temperature)]
        self.targets = []    # [(timestamp, scheduled target)], may be empty
        self.span = (0.0, 1.0)
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
            rgb(SEPARATOR).setFill()
            AK.NSRectFill(AK.NSMakeRect(0, round(y(v)), plot_w, 1))
            text = f"{v:.0f}°" if float(v).is_integer() else f"{v:.1f}°"
            attributed([(text, 9, "mono-regular", TEXT_3)]).drawAtPoint_((plot_w + 6, y(v) - 6))

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
            rgb(AMBER, 0.18).setFill()
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
            rgb(AMBER, 0.8).setStroke()
            step.stroke()

        # the room temperature
        line = AK.NSBezierPath.bezierPath()
        for i, (ts, v) in enumerate(self.points):
            (line.moveToPoint_ if i == 0 else line.lineToPoint_)((x(ts), y(v)))
        line.setLineWidth_(1.5)
        line.setLineJoinStyle_(AK.NSLineJoinStyleRound)
        line.setLineCapStyle_(AK.NSLineCapStyleRound)
        rgb(TEXT).setStroke()
        line.stroke()
        ts, v = self.points[-1]
        rgb(TEXT).setFill()
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
        self.panes = {
            "control": self.control,
            "settings": self.settings,
            "signin": self.signin,
            "code": self.code,
        }
        for view in self.panes.values():
            self.root.addSubview_(view)
        self._build_control()
        self._build_settings()
        self._build_signin()
        self._build_code()
        self.show_pane("control", resize=False)

    # ------------------------------------------------------------- control pane
    def _build_control(self):
        c = self.control

        # header
        c.addSubview_(make_label([("Home", 14, "semibold", TEXT)], 16, 16, 200, 17))
        self.status_label = make_label([("", 11, "regular", TEXT_2)], 16, 37, 240, 13)
        c.addSubview_(self.status_label)

        self.settings_btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(276, 19, 28, 28))
        self.settings_btn.configure(6, None, SURFACE)
        self.settings_btn.callback = lambda: self.show_pane("settings")
        self.settings_btn.addSubview_(icon_view("ellipsis", TEXT_2, 16, 6, 6))
        c.addSubview_(self.settings_btn)

        # dial
        self.dial_view = AK.NSImageView.alloc().initWithFrame_(AK.NSMakeRect(38, 62, 244, 244))
        self.dial_view.setImageScaling_(AK.NSImageScaleProportionallyUpOrDown)
        c.addSubview_(self.dial_view)
        c.addSubview_(make_label([("10°", 10, "regular", TEXT_3)], 65, 281, 30, 12))
        c.addSubview_(make_label([("30°", 10, "regular", TEXT_3)], 235, 281, 30, 12))
        self.target_caption = make_label(
            [("TARGET", 9, "medium", TEXT_3, 0.6)], 38, 124, 244, 11, "center"
        )
        c.addSubview_(self.target_caption)
        # same centre as the design's 75pt box, a little taller so SF Pro never clips.
        # The number and a raised °; render() lays them out.
        self.number_group = FlippedView.alloc().initWithFrame_(AK.NSMakeRect(38, 136, 244, 87))
        self.target_label = make_label([("--", 68, "light", TEXT)], 0, 0, 244, 87, "center")
        self.degree_label = make_label([("°", DEGREE_PT, "light", TEXT)], 0, 0, 0, 0, "center")
        for view in (self.target_label, self.degree_label):
            self.number_group.addSubview_(view)
        c.addSubview_(self.number_group)
        self.current_label = make_label([("Current --", 12, "regular", TEXT_2)], 38, 227, 244, 15, "center")
        c.addSubview_(self.current_label)
        self.countdown_label = make_label([("", 11, "regular", AMBER)], 38, 264, 244, 13, "center")
        c.addSubview_(self.countdown_label)

        # click the number to type a target in its place
        self.number_hit = NumberHit.alloc().initWithFrame_(AK.NSMakeRect(88, 140, 144, 80))
        self.number_hit.callback = self.begin_edit
        c.addSubview_(self.number_hit)
        self.target_field = self._number_field()
        c.addSubview_(self.target_field)

        # temperature controls
        # on one row with the set button, as in the design (Temperature controls, 3:841)
        self.minus_btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 318, 36, 36))
        self.minus_btn.configure(18, SURFACE, "#3a3c40", BORDER)
        self.minus_btn.callback = lambda: (self.end_edit(), self.app.nudge(-STEP))
        self.minus_btn.addSubview_(icon_view("minus", TEXT, 16, 10, 10))
        c.addSubview_(self.minus_btn)

        self.set_btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(100, 320, 120, 32))
        self.set_btn.configure(8, "#3c3e40", "#444649")  # 10% white over the popover, 14% on hover
        self.set_btn.callback = self.begin_edit
        self.set_btn.addSubview_(
            make_label([("Set temperature", 12, "medium", TEXT)], 0, 8.5, 120, 15, "center")
        )
        c.addSubview_(self.set_btn)

        self.plus_btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(268, 318, 36, 36))
        self.plus_btn.configure(18, SURFACE, "#3a3c40", BORDER)
        self.plus_btn.callback = lambda: (self.end_edit(), self.app.nudge(STEP))
        self.plus_btn.addSubview_(icon_view("plus", TEXT, 16, 10, 10))
        c.addSubview_(self.plus_btn)

        # boost
        self.boost_btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 370, 288, 36))
        self.boost_btn.callback = lambda: (self.end_edit(), self.app.toggle_boost())
        self.boost_icon = icon_view("timer", TEXT, 14, 88, 11)
        self.boost_btn.addSubview_(self.boost_icon)
        self.boost_text = make_label([("Boost for 1 hour", 12, "medium", TEXT)], 109, 10.5, 140, 15)
        self.boost_btn.addSubview_(self.boost_text)
        c.addSubview_(self.boost_btn)
        self.note_label = make_label([("", 10, "regular", TEXT_3)], 16, 416, 288, 12, "center")
        c.addSubview_(self.note_label)

        # the last day
        c.addSubview_(self._separator(440))
        c.addSubview_(make_label([("LAST 24 HOURS", 9, "medium", TEXT_3, 0.6)], 16, 455, 140, 11))
        self.history_summary = make_label([("", 10, "regular", TEXT_3)], 130, 454, 174, 12, "right")
        c.addSubview_(self.history_summary)
        self.history_chart = HistoryChart.alloc().initWithFrame_(AK.NSMakeRect(16, 474, 288, 68))
        c.addSubview_(self.history_chart)
        self.history_note = make_label([("Loading…", 10, "regular", TEXT_3)], 16, 500, 262, 12, "center")
        c.addSubview_(self.history_note)
        self.history_hours = []
        for _ in range(5):
            label = make_label([("", 9, "mono-regular", TEXT_3)], 0, 546, 40, 11, "center")
            self.history_hours.append(label)
            c.addSubview_(label)

    # ------------------------------------------------------------- typing a target
    EDIT_HINT = ("Type a temperature, then press Return. Esc cancels.", TEXT_3)

    def _number_field(self):
        """Editable twin of the big target number, shown in its place while typing."""
        tf = AK.NSTextField.alloc().initWithFrame_(AK.NSMakeRect(38, 136, 244, 87))
        tf.setBezeled_(False)
        tf.setBordered_(False)
        tf.setDrawsBackground_(False)
        tf.setEditable_(True)
        tf.setSelectable_(True)
        tf.setFocusRingType_(AK.NSFocusRingTypeNone)
        tf.setAlignment_(AK.NSTextAlignmentCenter)
        tf.setFont_(font(68, "light"))
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
        if not st or st["mode"] == "OFF" or not self.number_hit.enabled:
            return
        if not self.editing:
            self.editing = True
            self.edit_confirm = None
            t = float(st["target"])
            self.target_field.setStringValue_(f"{int(t)}" if t.is_integer() else f"{t:.1f}")
            self.number_group.setHidden_(True)
            self.target_field.setHidden_(False)
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
        self.number_group.setHidden_(False)
        # after Return the field is still busy, so drop focus a moment later
        AppHelper.callAfter(self._drop_edit_focus)
        if self.state:
            self._render_boost(self.state)

    def _drop_edit_focus(self):
        if not self.editing:
            self.panel.makeFirstResponder_(self.panel)

    def _select_edit(self):
        self.target_field.selectText_(None)
        editor = self.target_field.currentEditor()
        if editor is not None:  # amber caret and selection instead of the system blue
            editor.setInsertionPointColor_(rgb(AMBER))
            editor.setSelectedTextAttributes_({AK.NSBackgroundColorAttributeName: rgb(AMBER, 0.28)})

    def _set_edit_note(self, note):
        self.edit_note = note
        set_label(self.note_label, [(note[0], 10, "regular", note[1])], "center")

    def _edit_changed(self):
        """Keep to digits and one decimal mark, at most 4 characters."""
        text = str(self.target_field.stringValue())
        clean = "".join(ch for ch in text if ch.isdigit() or ch in ".,")[:4]
        if clean != text:
            self.target_field.setStringValue_(clean)
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
        box = Box.alloc().initWithFrame_(AK.NSMakeRect(16, y, 288, 1))
        box.colour = rgb(SEPARATOR)
        return box

    def _build_settings(self):
        s = self.settings
        back = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 19.5, 24, 28))
        back.configure(6, None, SURFACE)
        back.callback = lambda: self.show_pane("control")
        back.addSubview_(icon_view("chevron-left", TEXT, 16, 4, 6))
        s.addSubview_(back)
        s.addSubview_(make_label([("Settings", 14, "semibold", TEXT)], 50, 25, 200, 17))
        s.addSubview_(self._separator(63))

        s.addSubview_(make_label([("MODE", 10, "regular", TEXT_3)], 16, 76, 288, 12))
        self.mode_rows = {}
        modes = (
            ("SCHEDULE", "Scheduled", "Follow your thermostat schedule", "calendar-clock"),
            ("MANUAL", "Manual", "Hold a set temperature", "hand"),
            ("OFF", "Off", "Turn heating off", "power"),
        )
        for i, (key, name, desc, icon) in enumerate(modes):
            row = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 94 + 53 * i, 288, 51))
            row.configure(8, None, "#2e3033")
            row.callback = (lambda k=key: self.app.set_mode(k))
            row_icon = icon_view(icon, TEXT_2, 16, 10, 17.5)
            row.addSubview_(row_icon)
            name_label = make_label([(name, 12, "regular", TEXT)], 36, 10, 200, 15)
            row.addSubview_(name_label)
            row.addSubview_(make_label([(desc, 10, "regular", TEXT_3)], 36, 29, 220, 12))
            check = icon_view("check", TEXT, 14, 264, 18.5)
            row.addSubview_(check)
            s.addSubview_(row)
            self.mode_rows[key] = (row, row_icon, icon, name_label, name, check)

        s.addSubview_(self._separator(263))

        refresh = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 276, 288, 34))
        refresh.configure(8, None, "#2e3033")
        refresh.callback = lambda: self.app.refresh(user=True)
        refresh.addSubview_(icon_view("refresh-cw", TEXT_2, 15, 10, 9.5))
        refresh.addSubview_(make_label([("Refresh", 12, "regular", TEXT)], 35, 9.5, 149, 15))
        self.updated_label = make_label([("", 10, "regular", TEXT_3)], 144, 11, 134, 12, "right")
        refresh.addSubview_(self.updated_label)
        s.addSubview_(refresh)

        s.addSubview_(self._separator(322))

        sign_out = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 335, 288, 34))
        sign_out.configure(8, None, "#2e3033")
        sign_out.callback = lambda: self.app.sign_out()
        sign_out.addSubview_(icon_view("log-out", TEXT_2, 15, 10, 9.5))
        sign_out.addSubview_(make_label([("Sign out", 12, "regular", TEXT)], 35, 9.5, 243, 15))
        s.addSubview_(sign_out)

        quit_row = Hit.alloc().initWithFrame_(AK.NSMakeRect(16, 369, 288, 34))
        quit_row.configure(8, None, "#2e3033")
        quit_row.callback = lambda: AK.NSApp.terminate_(None)
        quit_row.addSubview_(icon_view("power", TEXT_2, 15, 10, 9.5))
        quit_row.addSubview_(make_label([(f"Quit {APP_NAME}", 12, "regular", TEXT)], 35, 9.5, 215, 15))
        quit_row.addSubview_(make_label([("⌘Q", 10, "regular", TEXT_3)], 228, 11, 50, 12, "right"))
        s.addSubview_(quit_row)

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
        elif self.pane == "code":
            self._cancel_code()
        else:
            self.hide()

    def show_pane(self, pane, resize=True):
        self.end_edit()
        self.pane = pane
        self._clear_hover()
        for name, view in self.panes.items():
            view.setHidden_(name != pane)
        glass = pane in ("signin", "code")
        self.bg.glass = glass
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
            set_label(self.status_label, [(message or "", 11, "regular", TEXT_2)])
            return

        off = st["mode"] == "OFF"
        boosting = st["boost"]
        heating = st["heating"]
        amber = (heating and not off) or boosting
        mode_name = "Scheduled" if st["mode"] == "SCHEDULE" else st["mode"].title()
        target = st["target"]
        heat = heat_hex(target)  # amber, shading to red for high targets

        # status line
        if message:
            status, colour = message, TEXT_2
        elif boosting:
            status, colour = "Heating · 1-hour boost", heat
        elif off:
            status, colour = "Heating off", TEXT_2
        elif heating and st.get("next_change") and st["mode"] == "SCHEDULE":
            status, colour = f"Heating · {mode_name} · Until {when_text(st['next_change'], clock_only=True)}", heat
        elif heating:
            status, colour = f"Heating · {mode_name}", heat
        elif st.get("next_change") and st["mode"] == "SCHEDULE":
            status, colour = f"Idle · {mode_name} · On {when_text(st['next_change'])}", TEXT_2
        else:
            status, colour = f"Idle · {mode_name}", TEXT_2
        set_label(self.status_label, [(status, 11, "regular", colour)])

        # dial and numbers
        accent = heat if amber else TEXT
        png = dial.dial_png(
            round(target * 4) / 4.0, rgb_tuple(heat) if amber else None, show_indicator=not off
        )
        self.dial_view.setImage_(ns_image(png, 244))
        if off:
            self._set_number("Off", TEXT_2, unit=False)
            set_label(self.target_caption, [("HEATING", 9, "medium", TEXT_3, 0.6)], "center")
        else:
            num = f"{int(target)}" if float(target).is_integer() else f"{target:.1f}"
            self._set_number(num, accent)
            set_label(self.target_caption, [("TARGET", 9, "medium", TEXT_3, 0.6)], "center")
        set_label(
            self.current_label,
            [("Current ", 12, "regular", TEXT_2), (f"{st['current']:.1f}°C", 12, "medium", TEXT)],
            "center",
        )

        # +/- availability
        for btn in (self.minus_btn, self.plus_btn, self.set_btn):
            btn.enabled = not off
            btn.setAlphaValue_(0.4 if off else 1.0)

        # boost button, countdown, note
        self._render_boost(st)

        # settings pane checkmarks
        for key, (row, row_icon, icon, name_label, name, check) in self.mode_rows.items():
            selected = key == st["mode"]
            row.fill = rgb(SURFACE) if selected else None
            row.setNeedsDisplay_(True)
            check.setHidden_(not selected)
            set_icon(row_icon, icon, TEXT if selected else TEXT_2, 16)
            set_label(name_label, [(name, 12, "medium" if selected else "regular", TEXT)])
        if self.pane == "settings":
            self._refresh_updated_label()
        self._set_dummy(dummy, off)

    def _set_number(self, text, colour, unit=True):
        """The big number, with ° level with the top of the digits."""
        parts = [(text, 68, "light", colour)]
        set_label(self.target_label, parts, "center")
        self.degree_label.setHidden_(not unit)
        if not unit:
            self.target_label.setFrame_(AK.NSMakeRect(0, 0, 244, 87))
            return
        num_w = text_width(parts)
        deg_w = text_width([("°", DEGREE_PT, "light", colour)])
        x = (244 - (num_w + DEGREE_GAP + deg_w)) / 2.0
        # text fields pad their text by 2pt each side
        self.target_label.setFrame_(AK.NSMakeRect(x - 2, 0, num_w + 4, 87))
        col_x = x + num_w + DEGREE_GAP
        self.degree_label.setFrame_(AK.NSMakeRect(col_x - 6, DEGREE_Y, deg_w + 12, DEGREE_PT * 1.3))
        set_label(self.degree_label, [("°", DEGREE_PT, "light", colour)], "center")

    def set_history(self, points, targets, error=None):
        """Fill the last-24-hours chart. points: [(timestamp, temperature)]."""
        if error and self.history_chart.points:
            return  # keep the chart we have; the next try may work
        end = time.time()
        start = end - HISTORY_HOURS * 3600
        points = [p for p in points if p[0] >= start]
        targets = [p for p in targets if p[0] >= start]
        self.history_chart.set_data(points, targets, start, end)
        if not points:
            set_label(self.history_note, [(error or "No readings yet", 10, "regular", TEXT_3)], "center")
            self.history_note.setHidden_(False)
            set_label(self.history_summary, [("", 10, "regular", TEXT_3)], "right")
        else:
            self.history_note.setHidden_(True)
            temps = [v for _, v in points]
            set_label(self.history_summary, [
                ("Low ", 10, "regular", TEXT_3), (f"{min(temps):.1f}°", 10, "medium", TEXT_2),
                ("   High ", 10, "regular", TEXT_3), (f"{max(temps):.1f}°", 10, "medium", TEXT_2),
            ], "right")
        # hour marks every 6 hours on the clock, and "Now" at the right
        plot_w = 288 - 26
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
            cx = 16 + (ts - start) / (end - start) * plot_w
            label.setFrame_(AK.NSMakeRect(cx - 20, 546, 40, 11))
            set_label(label, [(text, 9, "mono-regular", TEXT_3)], "center")
            label.setHidden_(False)

    def _render_boost(self, st):
        boosting = st["boost"]
        if boosting:
            self.boost_btn.configure(8, AMBER_SURFACE, "#47382d", AMBER, 0.5)
            label = "Cancel boost"
            colour = AMBER
            set_icon(self.boost_icon, "timer-off", AMBER, 14)
        else:
            self.boost_btn.configure(8, SURFACE, "#3a3c40", BORDER, 1.0)
            label = "Boost for 1 hour"
            colour = TEXT
            set_icon(self.boost_icon, "timer", TEXT, 14)
        parts = [(label, 12, "medium", colour)]
        w = text_width(parts)
        total = 14 + 7 + w
        x0 = (288 - total) / 2.0
        self.boost_icon.setFrame_(AK.NSMakeRect(x0, 11, 14, 14))
        self.boost_text.setFrame_(AK.NSMakeRect(x0 + 21, 10.5, w + 8, 15))
        set_label(self.boost_text, parts)

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
        note_colour = TEXT_3
        if st["mode"] != "OFF" and st["target"] >= VERY_HIGH_TEMP:
            note, note_colour = "Very high. Not advised for babies or older people.", RED
        elif st["mode"] != "OFF" and st["target"] >= WARN_TEMP:
            note, note_colour = "High setting. Each degree costs about 10% more to heat.", heat_hex(st["target"])
        if self.editing and self.edit_note:
            note, note_colour = self.edit_note
        set_label(self.note_label, [(note, 10, "regular", note_colour)], "center")
        self.tick()

    def tick(self):
        """Once a second: refresh the boost countdown."""
        st = self.state
        if st and st["boost"] and st.get("boost_end"):
            left = max(0, int(st["boost_end"] - time.time()))
            text = f"{left // 60:02d}:{left % 60:02d} remaining"
        else:
            text = ""
        set_label(self.countdown_label, [(text, 11, "mono-regular", AMBER)], "center")

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
        parent.addSubview_(make_label([(label, 11, "medium", TEXT_2)], 20, y, 280, 13))
        box = FieldBox.alloc().initWithFrame_(AK.NSMakeRect(20, y + 19.5, 280, 36))
        box.glass = True
        tf = self._text_field(10, 260 - right_pad, secure, placeholder, on_return)
        box.field = tf
        box.addSubview_(tf)
        parent.addSubview_(box)
        return tf

    def _primary_button(self, parent, y, text, callback):
        btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(20, y, 280, 36))
        btn.configure(8, "#e8e7e4", "#f6f5f3")
        btn.callback = callback
        label = make_label([(text, 12, "semibold", "#25282c")], 0, 10.5, 280, 15, "center")
        btn.addSubview_(label)
        parent.addSubview_(btn)
        return btn, label

    def _quit_button(self):
        """Top right of the sign-in panes, so you can quit before signing in."""
        btn = Hit.alloc().initWithFrame_(AK.NSMakeRect(273, 19, 28, 28))
        btn.configure(6, None, SURFACE)
        btn.callback = lambda: AK.NSApp.terminate_(None)
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
        sep = Box.alloc().initWithFrame_(AK.NSMakeRect(20, 68, 280, 1))
        sep.colour = rgb("#ffffff", 0.08)
        s.addSubview_(sep)

        s.addSubview_(make_label([("Sign in", 20, "medium", TEXT)], 20, 89, 280, 24))
        self.signin_msg = make_label([(SIGNIN_HINT, 11, "regular", TEXT_2)], 20, 119, 280, 30, wrap=True)
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
        self.password_plain = self._text_field(10, 230, False, "Password", self._submit_signin)
        self.password_plain.setHidden_(True)
        box.addSubview_(self.password_plain)
        self.password_visible = False
        eye = Hit.alloc().initWithFrame_(AK.NSMakeRect(248, 6, 24, 24))
        eye.configure(6, None, "#2f3237")
        eye.callback = self._toggle_password
        self.eye_icon = icon_view("eye", TEXT_2, 15, 4.5, 4.5)
        eye.addSubview_(self.eye_icon)
        box.addSubview_(eye)
        self.email_field.setNextKeyView_(self.password_field)
        self.password_field.setNextKeyView_(self.email_field)

        self.signin_btn, self.signin_btn_label = self._primary_button(
            s, 298, "Sign in", self._submit_signin
        )
        forgot = Hit.alloc().initWithFrame_(AK.NSMakeRect(90, 342, 140, 20))
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
        back.addSubview_(icon_view("chevron-left", TEXT, 16, 4, 6))
        s.addSubview_(back)
        s.addSubview_(make_label([("Verification", 14, "semibold", TEXT)], 54, 25.5, 200, 17))
        s.addSubview_(self._quit_button())
        sep = Box.alloc().initWithFrame_(AK.NSMakeRect(20, 68, 280, 1))
        sep.colour = rgb("#ffffff", 0.08)
        s.addSubview_(sep)
        self.code_field = self._field(
            s, 88, "Verification code", "6-digit code", on_return=self._submit_code
        )
        self.code_msg = make_label(
            [("Enter the code Hive texted you.", 11, "regular", TEXT_2)], 20, 154, 280, 30, wrap=True
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
        for btn in (self.minus_btn, self.plus_btn, self.set_btn, self.number_hit):
            btn.enabled = not (dummy or off)
        self.panel.invalidateCursorRectsForView_(self.number_hit)
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


def short_error(exc, where="refresh"):
    """A few words for the 240pt status line. The full error goes to LOG_PATH."""
    name = type(exc).__name__
    low = str(exc).lower()
    if name in ("HiveReauthRequired", "HiveInvalidDeviceAuthentication") or "unauthor" in low:
        return "Hive wants you to sign in again"
    if "timed out" in low or "timeout" in name.lower() or "connection" in low:
        return "Couldn't reach Hive. Retrying in a minute"
    if where == "refresh":  # refreshes repeat every REFRESH_SECONDS
        return "Hive isn't responding. Retrying in a minute"
    return "Hive couldn't make that change. Try again"


class HiveApp(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, title=APP_NAME, quit_button=None)
        self.session = None
        self.device = None
        self.lock = threading.Lock()
        self.nudge_lock = threading.Lock()
        self.last_target = None
        self.pending_target = None
        self._send_timer = None
        self.last_update = None
        self.refreshing = False
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
        self.ui = None
        self.handler = None
        self.button = None

        self.timer = rumps.Timer(lambda _: self.refresh(), REFRESH_SECONDS)
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
            self.device = climate[0]
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

    def sign_out(self):
        for key in ("username", "password", "device"):
            try:
                keyring.delete_password(SERVICE, key)
            except keyring.errors.PasswordDeleteError:
                pass
        self.session = None
        self.device = None
        self.state = None
        self.last_target = None
        self.history_at = 0.0
        self._set_bar(None)
        self._show_message("Sign in to see your heating")
        self.ui.show_signed_out()

    def _show_message(self, text):
        if self.ui is None:
            return
        if self.state is None:
            AppHelper.callAfter(self.ui.render, DUMMY_STATE, text, True)
        else:
            AppHelper.callAfter(self.ui.render, self.state, text)

    def _show_error(self, where, exc):
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
                self.session.updateData(self.device)
                current = float(h.getCurrentTemperature(self.device))
                target = float(h.getTargetTemperature(self.device))
                mode = (h.getMode(self.device) or "SCHEDULE").upper()
                boost = h.getBoostStatus(self.device) == "ON"
                heating = h.getState(self.device) == "ON"
                minutes = None
                if boost:
                    try:
                        minutes = int(h.getBoostTime(self.device))
                    except Exception:  # noqa: BLE001
                        minutes = None
                next_change = None  # when the schedule next turns the heating on, or off if it is on
                if mode == "SCHEDULE":
                    try:
                        product = self.session.data.products[self.device["hiveID"]]
                        next_change = next_heat_on(product["state"]["schedule"], current, on=not heating)
                    except Exception:  # noqa: BLE001
                        next_change = None  # the schedule is a nice-to-have; never fail a refresh on it
            if self.pending_target is not None:  # a tap is waiting to be sent; keep it
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
            }
            if seq != self.action_seq:  # read before the latest change; don't undo it on screen
                self.refresh_again = True
            else:
                AppHelper.callAfter(self._apply_state, st)
        except Exception as exc:  # noqa: BLE001
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
            AppHelper.callAfter(self.ui.set_history, points, targets)
        except Exception:  # noqa: BLE001
            log_error("history")
            self.history_at = time.time() - HISTORY_SECONDS + 120  # try again in a couple of minutes
            AppHelper.callAfter(self.ui.set_history, [], [], "History isn't available right now")
        finally:
            self.history_busy = False

    # ---------- controls
    def _queue_target(self, temp):
        """Show the new target at once, then send it to Hive after a short pause.

        Every tap restarts the pause, so a run of taps becomes one request
        with the final value. That keeps the UI quick and stops Hive's
        servers from rejecting a burst of calls.
        """
        temp = max(MIN_TEMP, min(MAX_TEMP, round(temp * 2) / 2))
        with self.nudge_lock:
            self.pending_target = temp
            self.last_target = temp
            if self._send_timer is not None:
                self._send_timer.cancel()
            self._send_timer = threading.Timer(SEND_DELAY, self._flush_target)
            self._send_timer.daemon = True
            self._send_timer.start()
        if self.state:
            st = dict(self.state)
            st["target"] = temp
            st["heating"] = st["current"] < temp and st["mode"] != "OFF"
            self._apply_state(st)  # called on the main thread

    def _flush_target(self):
        with self.nudge_lock:
            temp = self.pending_target
            self.pending_target = None
            self._send_timer = None
        if temp is None:
            return
        try:
            ok = self._send_target(temp)
            if ok is False:  # Hive turned the change down; try once more
                time.sleep(1.5)
                try:
                    self.session.hiveRefreshTokens(True)  # force a fresh login token
                except Exception:  # noqa: BLE001
                    pass
                ok = self._send_target(temp)
            if ok is False:
                self._show_message("Hive didn't accept that change. Try again.")
                time.sleep(2)
            self.refresh(user=True)
        except Exception as exc:  # noqa: BLE001
            self._show_error("set temperature", exc)

    def _send_target(self, temp):
        with self.lock:
            return self.session.heating.setTargetTemperature(self.device, temp)

    def nudge(self, delta):
        if not self.session:
            return
        if self.state and self.state["mode"] == "OFF":
            return
        base = self.pending_target
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
        if self.state["boost"]:
            st["boost"] = False
            st["boost_end"] = None
            self.boost_end = None
            self._apply_state(st)

            def cancel():
                try:
                    with self.lock:
                        self.session.heating.setBoostOff(self.device)
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
                    self.session.heating.setBoostOn(self.device, BOOST_MINUTES, temp)
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

        def work():
            try:
                with self.lock:
                    self.session.heating.setMode(self.device, mode)
                self.refresh(user=True)
            except Exception as exc:  # noqa: BLE001
                self._show_error("set mode", exc)

        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    HiveApp().run()
