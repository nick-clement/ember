"""Render assets/AppIcon.svg onto Apple's macOS icon grid: 824pt body in a 1024 canvas, with a soft shadow."""
import re, sys, AppKit as AK, Foundation as F
svg_path, out = sys.argv[1], sys.argv[2]
src = open(svg_path).read()
fill = re.search(r'<rect[^>]*fill="(#[0-9A-Fa-f]{6})"', src).group(1)
glyph_svg = re.sub(r'<rect[^>]*/>', '', src)  # the letter alone, on transparent
data = F.NSData.dataWithBytes_length_(glyph_svg.encode(), len(glyph_svg.encode()))
glyph = AK.NSImage.alloc().initWithData_(data)
assert glyph is not None, "SVG not readable"
S, B = 1024, 824
o = (S - B) / 2
rep = AK.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(None, S, S, 8, 4, True, False, AK.NSDeviceRGBColorSpace, 0, 0)
AK.NSGraphicsContext.saveGraphicsState()
AK.NSGraphicsContext.setCurrentContext_(AK.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
body = AK.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(AK.NSMakeRect(o, o + 10, B, B), 185, 185)
AK.NSGraphicsContext.saveGraphicsState()
sh = AK.NSShadow.alloc().init(); sh.setShadowOffset_((0, -10)); sh.setShadowBlurRadius_(24)
sh.setShadowColor_(AK.NSColor.colorWithWhite_alpha_(0, 0.3)); sh.set()
h = int(fill[1:], 16)
AK.NSColor.colorWithSRGBRed_green_blue_alpha_(((h >> 16) & 255) / 255, ((h >> 8) & 255) / 255, (h & 255) / 255, 1).setFill()
body.fill()
AK.NSGraphicsContext.restoreGraphicsState()
glyph.drawInRect_fromRect_operation_fraction_(AK.NSMakeRect(o, o + 10, B, B), AK.NSZeroRect, AK.NSCompositingOperationSourceOver, 1.0)
AK.NSGraphicsContext.restoreGraphicsState()
rep.representationUsingType_properties_(AK.NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(out, True)
print("ok")
