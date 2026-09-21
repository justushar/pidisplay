#!/usr/bin/env python3
"""Self-checks for the pure logic. No hardware, no framework.

    python3 test_display.py
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Hermetic: the tests own the media directory, and nothing touches real hardware.
_MEDIA = tempfile.mkdtemp(prefix="pidisplay-test-")
os.environ["FB_DEV"] = "/dev/null"
os.environ["MEDIA_DIR"] = _MEDIA

from PIL import Image, ImageDraw  # noqa: E402

import display as D  # noqa: E402

PASSED = 0


def check(name, cond):
    global PASSED
    if not cond:
        print(f"FAIL - {name}")
        sys.exit(1)
    PASSED += 1
    print(f"ok   - {name}")


def px(frame, x, y):
    """The 16-bit little-endian pixel at (x, y)."""
    off = (y * D.W + x) * 2
    return frame[off] | (frame[off + 1] << 8)


def solid(rgb):
    return D.pack(Image.new("RGB", (D.W, D.H), rgb))


# ---- geometry -------------------------------------------------------------

check("frame is 307200 bytes", D.FRAME == 307200)
check("stride has no row padding", D.FRAME == D.W * D.H * 2 and D.W * 2 == 960)

# ---- RGB565 packing -------------------------------------------------------

check("pure red  -> 0xF800", px(solid((255, 0, 0)), 0, 0) == 0xF800)
check("pure green -> 0x07E0", px(solid((0, 255, 0)), 0, 0) == 0x07E0)
check("pure blue -> 0x001F", px(solid((0, 0, 255)), 0, 0) == 0x001F)
check("white -> 0xFFFF", px(solid((255, 255, 255)), 0, 0) == 0xFFFF)
check("black -> 0x0000", px(solid((0, 0, 0)), 0, 0) == 0x0000)
check("mid grey keeps channel balance", px(solid((128, 128, 128)), 0, 0) == 0x8410)

# Rounding, not truncation: 7 is nearer 8 than 0, so it must not floor to zero.
# `7 >> 3` would give 0 and darken the whole image systematically.
check("near-black rounds up rather than truncating to zero",
      px(solid((7, 0, 0)), 0, 0) >> 11 == 1)
check("quantisation error stays within half a step",
      all(abs(round((px(solid((v, v, v)), 0, 0) >> 11) * 255 / 31) - v) <= 5
          for v in (0, 17, 64, 100, 190, 255)))
check("packed frame is exactly one frame", len(solid((1, 2, 3))) == D.FRAME)

# Red must land in the HIGH bits: this is the hardware fact verified on the
# panel with a three-bar pattern. If it ever flips, the panel goes wrong.
check("red occupies the top 5 bits", px(solid((255, 0, 0)), 0, 0) >> 11 == 0x1F)

# ---- fit ------------------------------------------------------------------

for w, h in ((4000, 3000), (12, 9), (480, 320), (1, 1), (2000, 10)):
    out = D.fit(Image.new("RGB", (w, h), (255, 255, 255)))
    check(f"fit({w}x{h}) -> 480x320", out.size == (D.W, D.H))

wide = D.fit(Image.new("RGB", (400, 100), (255, 255, 255)))
check("wide image is letterboxed, not stretched", wide.getpixel((240, 2)) == (0, 0, 0))
check("wide image fills the centre band", wide.getpixel((240, 160)) == (255, 255, 255))

tall = D.fit(Image.new("RGB", (100, 400), (255, 255, 255)))
check("tall image is pillarboxed", tall.getpixel((2, 160)) == (0, 0, 0))
check("tall image fills the centre column", tall.getpixel((240, 160)) == (255, 255, 255))

# cover: fill the panel edge to edge, cropping the overflow
for w, h in ((220, 392), (640, 640), (4000, 200), (7, 900), (480, 320)):
    out = D.fit(Image.new("RGB", (w, h), (255, 255, 255)), "cover")
    check(f"cover({w}x{h}) -> 480x320", out.size == (D.W, D.H))

cover_tall = D.fit(Image.new("RGB", (220, 392), (255, 255, 255)), "cover")
for x, y in ((2, 2), (477, 2), (2, 317), (477, 317), (240, 160)):
    check(f"cover leaves no letterbox at ({x},{y})",
          cover_tall.getpixel((x, y)) == (255, 255, 255))

# cover must crop, not squash: a centred marker stays centred and unstretched
marked = Image.new("RGB", (200, 400), (0, 0, 0))
ImageDraw.Draw(marked).rectangle([50, 150, 150, 250], fill=(255, 255, 255))
cm = D.fit(marked, "cover")
check("cover keeps the centre centred", cm.getpixel((240, 160)) == (255, 255, 255))
check("cover crops the top away", cm.getpixel((240, 2)) == (0, 0, 0))

check("an unknown fit mode behaves as contain",
      D.fit(Image.new("RGB", (100, 400), (255, 255, 255)), "nonsense").getpixel((2, 160))
      == (0, 0, 0))

# ---- pixel mode: nearest-neighbour at whole-number scale -----------------

check("120x80 scales up 4x exactly", D.pixel_target(120, 80) == (480, 320))
check("240x160 scales up 2x exactly", D.pixel_target(240, 160) == (480, 320))
check("480x320 stays 1:1", D.pixel_target(480, 320) == (480, 320))
check("a source just over panel size stays 1:1 for cropping",
      D.pixel_target(498, 331) == (498, 331))
check("1920x1080 divides down by 4", D.pixel_target(1920, 1080) == (480, 270))
check("960x640 divides down by 2", D.pixel_target(960, 640) == (480, 320))
check("a tiny source scales up by a whole factor",
      D.pixel_target(51, 54) == (51 * 5, 54 * 5))
check("a 1x1 source scales by the limiting dimension", D.pixel_target(1, 1) == (320, 320))

for w, h in ((120, 80), (498, 331), (1920, 1080), (51, 54), (1, 1), (7, 900), (4000, 9)):
    out = D.fit(Image.new("RGB", (w, h), (255, 255, 255)), "pixel")
    check(f"pixel({w}x{h}) -> 480x320", out.size == (D.W, D.H))

# The real proof of nearest-neighbour: a hard-edged source must come out
# containing ONLY its original colours. Any interpolation would blend greys in.
checker = Image.new("RGB", (120, 80), (0, 0, 0))
_d = ImageDraw.Draw(checker)
for _y in range(0, 80, 2):
    for _x in range(0, 120, 2):
        if (_x + _y) % 4 == 0:
            _d.rectangle([_x, _y, _x + 1, _y + 1], fill=(255, 255, 255))

pixel_out = set(D.fit(checker, "pixel").getdata())
check("pixel mode introduces no interpolated colours",
      pixel_out <= {(0, 0, 0), (255, 255, 255)})
check("pixel mode actually kept both colours",
      pixel_out == {(0, 0, 0), (255, 255, 255)})
# ...and contain, which uses Lanczos, demonstrably does blend.
check("contain does interpolate (so the above is a real difference)",
      len(set(D.fit(checker, "contain").getdata())) > 2)

check("pixel mode crops a near-panel-size source rather than resampling it",
      D.fit(Image.new("RGB", (498, 331), (255, 255, 255)), "pixel").getpixel((0, 0))
      == (255, 255, 255))

# ---- colour validation (user input reaches PIL) ---------------------------

check("accepts #rrggbb", D.colour("#a1b2c3", "#000000") == "#a1b2c3")
check("rejects a bare word", D.colour("red", "#000000") == "#000000")
check("rejects 3-digit hex", D.colour("#fff", "#000000") == "#000000")
check("rejects an injection attempt", D.colour("#fff; rm -rf /", "#000000") == "#000000")
check("rejects None", D.colour(None, "#123456") == "#123456")

# ---- text -----------------------------------------------------------------

check("empty text still yields a frame", len(D.render_text("")) == D.FRAME)
check("plain text yields a frame", len(D.render_text("hello panel")) == D.FRAME)
check("blank lines survive", len(D.render_text("a\n\nb")) == D.FRAME)
check("an unbreakable 500-char word does not crash", len(D.render_text("x" * 500)) == D.FRAME)
check("more lines than fit does not crash", len(D.render_text("line\n" * 60)) == D.FRAME)
check("large font does not crash", len(D.render_text("big", 96)) == D.FRAME)

draw = Image.new("RGB", (10, 10))
wrapped = D.wrap(ImageDraw.Draw(draw), "alpha beta gamma delta", D.font(20), 10_000)
check("wrap keeps every word when width is ample", " ".join(wrapped) == "alpha beta gamma delta")
narrow = D.wrap(ImageDraw.Draw(draw), "alpha beta gamma delta", D.font(20), 1)
check("wrap never drops words when width is tiny", sorted(" ".join(narrow).split()) ==
      ["alpha", "beta", "delta", "gamma"])

# ---- test pattern pins the verified channel order -------------------------

pattern = D.test_pattern()
check("test pattern: left third is red", px(pattern, 20, 20) == 0xF800)
check("test pattern: middle third is green", px(pattern, 240, 20) == 0x07E0)
check("test pattern: right third is blue", px(pattern, 460, 20) == 0x001F)

# ---- host stats (Linux only) ---------------------------------------------

if os.path.exists("/proc/stat"):
    s = D.stats()
    check("cpu percent within range", 0.0 <= s["cpu"] <= 100.0)
    check("memory percent within range", 0.0 < s["mem_pct"] <= 100.0)
    check("memory total looks sane", s["mem_total_mb"] > 16)
    check("disk percent within range", 0.0 <= s["disk_pct"] <= 100.0)
    check("uptime is positive", s["uptime"] > 0)
    check("three load figures", len(s["load"]) == 3)
    check("sysmon renders one frame", len(D.render_sysmon()) == D.FRAME)
else:
    print("skip - host stats (no /proc)")

# ---- API field coercion --------------------------------------------------

check("as_int passes a good value", D.as_int(12, 10, 1, 30) == 12)
check("as_int clamps high", D.as_int(999, 10, 1, 30) == 30)
check("as_int clamps low", D.as_int(-5, 10, 1, 30) == 1)
check("as_int accepts a numeric string", D.as_int("22", 10, 1, 30) == 22)
check("as_int falls back on junk", D.as_int("abc", 10, 1, 30) == 10)
check("as_int falls back on None", D.as_int(None, 10, 1, 30) == 10)
check("as_int falls back on a list", D.as_int([1], 10, 1, 30) == 10)

# ---- upload naming -------------------------------------------------------

check("keeps an ordinary name", D.upload_name("cat.gif") == "cat.gif")
check("keeps an uppercase extension", D.upload_name("clip.MP4").lower().endswith(".mp4"))
check("rejects an unsupported type", D.upload_name("payload.exe") is None)
check("rejects an empty name", D.upload_name("") is None)
check("rejects a bare extension-less name", D.upload_name("README") is None)

for hostile in ("../../etc/passwd.png", "/etc/shadow.png", "..\\..\\win.png",
                "a/b/c.gif", "图片.png", "   .png"):
    out = D.upload_name(hostile)
    check(f"{hostile!r} yields a usable, separator-free name",
          out is not None
          and "/" not in out and "\\" not in out and ".." not in out
          and os.path.splitext(out)[1].lower() in D.ALL_EXT
          and os.path.splitext(out)[0] != "")

# ---- frame-size guard ----------------------------------------------------

try:
    D.Screen().blit(b"\x00" * 10)
    check("short frame is rejected", False)
except ValueError:
    check("short frame is rejected", True)

# ---- panel framerate ceiling --------------------------------------------
# Writes to /dev/fb1 are deferred (~0.2ms), so the real limit is SPI bandwidth
# and has to be computed rather than observed.

check("ceiling is derived from SPI bandwidth", D.PANEL_FPS > 0)

# The bus divides the core clock by an even integer, so requested speeds snap
# down. Measured on this Pi: core = 400 MHz.
_saved_core = D.CORE_HZ
D.CORE_HZ = 400_000_000
check("16 MHz requested is really 15.38", abs(D.actual_spi_hz(16_000_000) - 15_384_615) < 1000)
check("32 MHz requested is really 28.57", abs(D.actual_spi_hz(32_000_000) - 28_571_428) < 1000)
check("48 MHz requested is really 40.00", D.actual_spi_hz(48_000_000) == 40_000_000)
check("40 and 48 MHz land on the same divisor",
      D.actual_spi_hz(40_000_000) == D.actual_spi_hz(48_000_000))
check("62 MHz requested is really 50.00", D.actual_spi_hz(62_000_000) == 50_000_000)
check("the divisor is always even",
      all((D.CORE_HZ / D.actual_spi_hz(r)) % 2 == 0
          for r in (16_000_000, 25_000_000, 32_000_000, 48_000_000, 62_000_000)))
check("the actual clock never exceeds what was asked for",
      all(D.actual_spi_hz(r) <= r
          for r in (16_000_000, 25_000_000, 32_000_000, 48_000_000, 62_000_000)))
check("an absurd request is clamped to the smallest even divisor",
      D.actual_spi_hz(10**12) == D.CORE_HZ / 2)
check("a zero or missing request yields nothing",
      D.actual_spi_hz(0) is None and D.actual_spi_hz(None) is None)
check("28.57 MHz gives an 11.6 fps ceiling",
      abs(D.actual_spi_hz(32_000_000) / 8 / D.FRAME - 11.62) < 0.05)
check("40 MHz gives a 16.3 fps ceiling",
      abs(D.actual_spi_hz(48_000_000) / 8 / D.FRAME - 16.28) < 0.05)
D.CORE_HZ = _saved_core
check("MAX_FPS is a whole number at or below the ceiling",
      isinstance(D.MAX_FPS, int) and 1 <= D.MAX_FPS <= D.PANEL_FPS)

check("an over-ceiling request clamps down", D.as_int(30, 6, 1, 6) == 6)
check("an under-ceiling request is left alone", D.as_int(3, 6, 1, 6) == 3)
check("junk falls back to the ceiling", D.as_int("fast", 6, 1, 6) == 6)

check("an explicit PANEL_FPS override is honoured",
      (os.environ.__setitem__("PANEL_FPS", "12.5"),
       abs(D.panel_ceiling() - 12.5) < 0.01,
       os.environ.pop("PANEL_FPS"))[1])
check("a junk PANEL_FPS override is ignored",
      (os.environ.__setitem__("PANEL_FPS", "quick"),
       D.panel_ceiling() > 0,
       os.environ.pop("PANEL_FPS"))[1])

# ---- picture tuning ------------------------------------------------------

check("tune defaults are sane",
      D.TUNE["gamma"] == 1.0 and D.TUNE["dither"] in D.DITHERS
      and D.TUNE["sharp"] in D.SCALERS)
check("bayer is the default dither (fastest and it dithers)",
      D.TUNE_DEFAULTS["dither"] == "bayer")

# ---- playback speed: slow instead of dropping frames ---------------------

check("a clip within the ceiling is untouched", D.playback_speed(10, 13) == 1.0)
check("a clip exactly at the ceiling is untouched", D.playback_speed(13, 13) == 1.0)
check("25 fps on a 13 fps panel halves", abs(D.playback_speed(25, 13) - 0.52) < 0.01)
check("33 fps on a 13 fps panel slows further",
      abs(D.playback_speed(100 / 3, 13) - 0.39) < 0.01)
check("25 fps on a 25 fps panel is untouched", D.playback_speed(25, 25) == 1.0)
check("unknown native rate is left alone", D.playback_speed(None, 13) == 1.0)
check("a zero native rate does not divide by zero", D.playback_speed(0, 13) == 1.0)
check("speed never drops below 0.1", D.playback_speed(10_000, 1) == 0.1)
# The point of the whole thing: slowed rate must fit under the ceiling.
for native in (14, 20, 25, 30, 50, 60):
    check(f"{native} fps slowed to {13} fps panel fits",
          native * D.playback_speed(native, 13) <= 13.001)

check("speed accepts auto", D.coerce_tune("speed", "auto") == "auto")
check("speed accepts a number", D.coerce_tune("speed", 0.5) == 0.5)
check("speed clamps above 1", D.coerce_tune("speed", 4) == 1.0)
try:
    D.coerce_tune("speed", "quick")
    check("speed rejects junk", False)
except ValueError:
    check("speed rejects junk", True)

check("coerce clamps gamma", D.coerce_tune("gamma", 99) == 4.0)
check("coerce accepts a numeric string", D.coerce_tune("saturation", "1.5") == 1.5)
for bad_key, bad_val in (("gamma", "loud"), ("saturation", None),
                         ("dither", "sprinkles"), ("sharp", "crayon"),
                         ("nonsense", 1)):
    try:
        D.coerce_tune(bad_key, bad_val)
        check(f"coerce rejects {bad_key}={bad_val!r}", False)
    except ValueError:
        check(f"coerce rejects {bad_key}={bad_val!r}", True)
check("a legacy boolean dither maps to bayer", D.coerce_tune("dither", True) == "bayer")
check("a legacy false dither maps to none", D.coerce_tune("dither", False) == "none")
check("gamma 1.0 is identity", list(D._gamma_lut(1.0)[[0, 128, 255]]) == [0, 128, 255])
check("gamma below 1 brightens midtones", D._gamma_lut(0.5)[128] > 128)
check("gamma above 1 darkens midtones", D._gamma_lut(2.0)[128] < 128)
check("gamma never clips the endpoints",
      (D._gamma_lut(2.5)[0], D._gamma_lut(2.5)[255]) == (0, 255))

_before = px(solid((128, 128, 128)), 0, 0)
D.TUNE["gamma"] = 0.5
_after = px(solid((128, 128, 128)), 0, 0)
D.TUNE["gamma"] = 1.0
check("gamma actually changes what gets packed", _after != _before)
check("packing is back to normal once gamma is reset",
      px(solid((128, 128, 128)), 0, 0) == _before)

D.TUNE_FILE = D.MEDIA / ".tune-test.json"
D.TUNE["gamma"], D.TUNE["sharp"] = 1.4, "bicubic"
D.save_tune()
D.TUNE.update(D.TUNE_DEFAULTS)
D.load_tune()
check("tune round-trips through disk",
      (D.TUNE["gamma"], D.TUNE["sharp"]) == (1.4, "bicubic"))
D.TUNE_FILE.write_text('{"gamma": "loud", "sharp": "crayon", "dither": true}')
D.TUNE.update(D.TUNE_DEFAULTS)
D.load_tune()
check("junk gamma falls back to the default", D.TUNE["gamma"] == 1.0)
check("an unknown scaler falls back to the default", D.TUNE["sharp"] in D.SCALERS)
check("a legacy boolean dither is migrated, not discarded", D.TUNE["dither"] == "bayer")
D.TUNE_FILE.write_text("{ not json")
D.TUNE.update(D.TUNE_DEFAULTS)
D.load_tune()
check("corrupt tune leaves defaults intact", D.TUNE == D.TUNE_DEFAULTS)
D.TUNE_FILE.unlink()
D.TUNE_FILE = D.MEDIA / ".tune.json"

check("the diagnostic pattern renders one frame", len(D.diag_pattern()) == D.FRAME)

# ---- per-file fit preferences --------------------------------------------

check("default fit is contain", D.fit_for("never-seen.gif") == "contain")
D.set_fit("a.gif", "cover")
check("a preference is stored", D.fit_for("a.gif") == "cover")
D.set_fit("a.gif", "contain")
check("a preference can be changed back", D.fit_for("a.gif") == "contain")
D.set_fit("b.gif", "cover")
check("preferences are independent per file",
      (D.fit_for("a.gif"), D.fit_for("b.gif")) == ("contain", "cover"))
check("no temp file is left behind",
      not (D.MEDIA / (D.FITS.name + ".tmp")).exists())

D.FITS.write_text('{"c.gif": "sideways", "d.gif": "cover"}')
check("an invalid stored mode is ignored", D.fit_for("c.gif") == "contain")
check("valid entries alongside it still load", D.fit_for("d.gif") == "cover")
D.FITS.write_text("{ not json")
check("corrupt preferences degrade to defaults", D.fit_for("d.gif") == "contain")
D.FITS.unlink()

# ---- library listing -----------------------------------------------------

Image.new("RGB", (40, 30), (10, 200, 10)).save(os.path.join(_MEDIA, "still.png"))
Image.new("RGB", (40, 30), (200, 10, 10)).save(os.path.join(_MEDIA, "one.gif"))

names = {m["name"] for m in D.library()}
check("library lists both files", names == {"still.png", "one.gif"})
check("a single-frame gif is treated as a still",
      [m["kind"] for m in D.library() if m["name"] == "one.gif"] == ["image"])

# ---- specs: canonicalise and validate ------------------------------------
# Video and slideshow sources are lazy generators, so none of this spawns
# ffmpeg or writes to the panel; only the canonical form is exercised.

canon = D.apply_spec({"mode": "image", "name": "still.png"}, remember=False)
check("image spec canonicalises",
      canon == {"mode": "image", "name": "still.png", "fit": "contain"})
check("image spec updates the screen status", D.screen.status.get("name") == "still.png")

check("an explicit fit is honoured",
      D.apply_spec({"mode": "image", "name": "still.png", "fit": "cover"},
                   remember=False)["fit"] == "cover")
check("an explicit fit becomes the remembered preference",
      D.fit_for("still.png") == "cover")
check("the remembered preference is reused when none is given",
      D.apply_spec({"mode": "image", "name": "still.png"}, remember=False)["fit"] == "cover")
check("a bogus fit falls back to the remembered preference",
      D.apply_spec({"mode": "image", "name": "still.png", "fit": "diagonal"},
                   remember=False)["fit"] == "cover")
check("library reports each item's fit",
      [m["fit"] for m in D.library() if m["name"] == "still.png"] == ["cover"])
D.set_fit("still.png", "contain")

# A GIF is routed down the video path, so its fps must respect the ceiling.
Image.new("RGB", (60, 40), (0, 0, 255)).save(
    os.path.join(_MEDIA, "anim.gif"), save_all=True,
    append_images=[Image.new("RGB", (60, 40), (255, 0, 0))], duration=100, loop=0)
check("a multi-frame gif is treated as motion", D.kind_of(D.MEDIA / "anim.gif") == "video")
check("an over-ceiling fps request is capped",
      D.apply_spec({"mode": "video", "name": "anim.gif", "fps": 30},
                   remember=False)["fps"] == D.MAX_FPS)
check("an under-ceiling fps request survives",
      D.apply_spec({"mode": "video", "name": "anim.gif", "fps": 1},
                   remember=False)["fps"] == 1)
check("slideshow fps is capped too",
      D.apply_spec({"mode": "slideshow", "fps": 30}, remember=False)["fps"] == D.MAX_FPS)

canon = D.apply_spec({"mode": "text", "text": "hi", "size": 9999,
                      "fg": "not-a-colour", "bg": "#112233"}, remember=False)
check("text size is clamped", canon["size"] == 96)
check("a bad colour falls back", canon["fg"] == "#ffffff")
check("a good colour is kept", canon["bg"] == "#112233")

check("sysmon spec canonicalises",
      D.apply_spec({"mode": "sysmon"}, remember=False) == {"mode": "sysmon"})
check("test spec canonicalises",
      D.apply_spec({"mode": "test"}, remember=False) == {"mode": "test"})
check("clear spec canonicalises",
      D.apply_spec({"mode": "clear"}, remember=False) == {"mode": "clear"})

for bad in ({}, {"mode": "nope"}, {"mode": "image"},
            {"mode": "image", "name": "missing.png"},
            {"mode": "image", "name": "../../etc/passwd"},
            {"mode": "video", "name": "/etc/shadow"}):
    try:
        D.apply_spec(bad, remember=False)
        check(f"rejects {bad!r}", False)
    except ValueError:
        check(f"rejects {bad!r}", True)

# ---- slideshow -----------------------------------------------------------

lib_names = sorted(m["name"] for m in D.library())
canon = D.apply_spec({"mode": "slideshow"}, remember=False)
check("slideshow fills names from the library", sorted(canon["names"]) == lib_names)
check("slideshow default interval is 8s", canon["seconds"] == 8)
check("slideshow status carries an item count",
      D.screen.status.get("count") == len(lib_names))
check("slideshow clamps an absurd interval",
      D.apply_spec({"mode": "slideshow", "seconds": 99999}, remember=False)["seconds"] == 3600)
check("slideshow shuffle flag survives",
      D.apply_spec({"mode": "slideshow", "shuffle": True}, remember=False)["shuffle"] is True)

_real_media = D.MEDIA
D.MEDIA = Path(tempfile.mkdtemp(prefix="pidisplay-empty-"))
try:
    D.apply_spec({"mode": "slideshow"}, remember=False)
    check("slideshow with an empty library is rejected", False)
except ValueError:
    check("slideshow with an empty library is rejected", True)
finally:
    shutil.rmtree(D.MEDIA, ignore_errors=True)
    D.MEDIA = _real_media

D.screen.detail = None
gen = D.slideshow_source(D.screen, ["still.png"], 0.01, 10, False)
check("slideshow yields a real frame", len(next(gen)) == D.FRAME)
check("slideshow reports the item it is on", D.screen.detail == "still.png")
gen.close()

gen = D.slideshow_source(D.screen, ["ghost.png", "still.png"], 0.01, 10, False)
check("slideshow skips a file that vanished", len(next(gen)) == D.FRAME)
gen.close()

gen = D.slideshow_source(D.screen, ["ghost.png"], 0.01, 10, False)
check("slideshow with only missing files shows a message", len(next(gen)) == D.FRAME)
try:
    next(gen)
    check("slideshow then gives up instead of spinning", False)
except StopIteration:
    check("slideshow then gives up instead of spinning", True)

# ---- persistence ---------------------------------------------------------

spec = {"mode": "slideshow", "names": ["still.png"], "seconds": 5,
        "fps": 10, "shuffle": True}
D.save_spec(spec)
check("state round-trips through disk", D.load_spec() == spec)
check("the state file never shows up in the library",
      all(m["name"] != D.STATE.name for m in D.library()))
check("no temp file is left behind",
      not (D.MEDIA / (D.STATE.name + ".tmp")).exists())

D.STATE.write_text("{ not json")
check("corrupt state loads as None", D.load_spec() is None)
D.STATE.unlink()
check("absent state loads as None", D.load_spec() is None)

check("apply_spec persists when asked",
      D.apply_spec({"mode": "image", "name": "still.png"}) == D.load_spec())

# ---- crash-loop breaker --------------------------------------------------

D.GUARD_HOLD = 0.05
D.save_spec({"mode": "image", "name": "still.png"})
D.disarm_guard()

check("a clean restore succeeds", D.restore_saved_spec() is True)
time.sleep(0.4)
check("a restore that holds up disarms the guard", not D.GUARD.exists())

# Simulate the device dying mid-restore: the marker is left behind.
D.GUARD.touch()
check("a restore is refused after an unsurvived boot", D.restore_saved_spec() is False)
check("the refusal clears the guard, so the next boot may try again",
      not D.GUARD.exists())
check("and the next boot does try again", D.restore_saved_spec() is True)
time.sleep(0.4)

# A spec that is merely invalid is a clean rejection, not a crash.
D.save_spec({"mode": "image", "name": "deleted-since.png"})
check("an unusable spec is refused", D.restore_saved_spec() is False)
check("an unusable spec leaves no guard behind", not D.GUARD.exists())

D.STATE.unlink()
check("no saved state means no restore", D.restore_saved_spec() is False)

shutil.rmtree(_MEDIA, ignore_errors=True)

print(f"\n{PASSED} checks passed")
