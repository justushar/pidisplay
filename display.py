#!/usr/bin/env python3
"""Display server for a 480x320 ILI9486 SPI panel on /dev/fb1.

Everything on the panel is one 307200-byte RGB565 frame. A single painter
thread owns the framebuffer; every content type is just an iterable of frames.
"""

from __future__ import annotations

import glob
import io
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
from flask import Flask, Response, jsonify, request, send_file
from PIL import Image, ImageDraw, ImageFont
from werkzeug.utils import secure_filename

W, H = 480, 320
FRAME = W * H * 2  # stride is exactly W*2 on this panel, so rows have no padding

FB = Path(os.environ.get("FB_DEV", "/dev/fb1"))
MEDIA = Path(os.environ.get("MEDIA_DIR", "/media"))
# Lives inside the media volume so it survives reboots without a second mount.
# Dot-prefixed and not a media extension, so it never shows in the library.
STATE = MEDIA / ".state.json"
# Set while a restore is being attempted and cleared once the service has
# survived a while. Finding it at startup means the last restore took the
# device down, so we must not repeat it. See main().
GUARD = MEDIA / ".restoring"
GUARD_HOLD = float(os.environ.get("GUARD_HOLD_SECONDS", 60))
# Per-file "contain" vs "cover" choices, so each item keeps its own framing.
FITS = MEDIA / ".fits.json"

# ponytail: this panel is verified true RGB565-LE. Flag kept because sibling
# ILI9486 boards ship BGR, and then it's a one-env-var fix instead of a patch.
BGR = os.environ.get("BGR", "0") == "1"

def _spi_hz() -> int | None:
    """The panel's SPI clock, straight from the device tree."""
    for pattern in ("/host-dt/soc/spi@*/tft*@0/spi-max-frequency",
                    "/proc/device-tree/soc/spi@*/tft*@0/spi-max-frequency"):
        for path in sorted(glob.glob(pattern)):
            try:
                return int.from_bytes(Path(path).read_bytes()[:4], "big")
            except (OSError, ValueError):
                continue
    return None


# The SPI block divides the core clock by an EVEN integer, so a requested speed
# snaps down to a discrete value: at 400MHz core, "32 MHz" is really 28.57.
# Computing the ceiling from the requested figure over-states it by ~12% and
# quietly reintroduces the judder the cap exists to prevent.
CORE_HZ = int(os.environ.get("CORE_HZ", 400_000_000))


def actual_spi_hz(requested: int | None) -> float | None:
    if not requested or requested <= 0:
        return None
    divisor = -(-CORE_HZ // requested)  # ceiling division
    divisor += divisor % 2              # must be even
    return CORE_HZ / max(2, divisor)


def panel_ceiling() -> float:
    """Frames per second the SPI bus can physically deliver.

    Writes to /dev/fb1 are deferred and return in ~0.2ms, so nothing downstream
    of us reveals this limit - it has to be computed. Feeding the panel faster
    than this does not show more motion, it just drops frames at irregular
    intervals, which reads as judder.
    """
    override = os.environ.get("PANEL_FPS")
    if override:
        try:
            return max(1.0, float(override))
        except ValueError:
            pass
    hz = actual_spi_hz(_spi_hz())
    if hz:
        return max(1.0, hz / 8 / FRAME)
    return actual_spi_hz(16_000_000) / 8 / FRAME  # tft35a default, ~6.3 fps


PANEL_FPS = panel_ceiling()
MAX_FPS = max(1, int(PANEL_FPS))

# Picture tuning. A 16-bit panel with its own gamma curve cannot be got right
# from first principles, so these are adjustable at runtime and persisted.
SCALERS = ("lanczos", "bicubic", "bilinear", "neighbor")
# Measured on this Pi at 480x320: bayer 38.6 fps, x_dither 31.4, a_dither 31.3,
# none 31.1, ed 14.3. Ordered dither is SIMD-accelerated in swscale, so it beats
# no dithering outright; error-diffusion is serial and halves throughput.
DITHERS = ("bayer", "a_dither", "x_dither", "none", "ed")
TUNE_FILE = MEDIA / ".tune.json"
TUNE_DEFAULTS = {
    "gamma": 1.0,        # <1 brightens midtones, >1 darkens
    "saturation": 1.0,
    "dither": "bayer",   # how to reduce 24-bit colour to the panel's 16-bit
    "sharp": "lanczos",  # ffmpeg scaler: lanczos | bicubic | bilinear | neighbor
    "speed": "auto",     # "auto" slows fast clips so no frame is dropped
}
TUNE = dict(TUNE_DEFAULTS)


def coerce_tune(key: str, value):
    """Validate one picture setting, or raise ValueError explaining why not."""
    if key in ("gamma", "saturation"):
        try:
            return max(0.1, min(4.0, float(value)))
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number")
    if key == "dither":
        if isinstance(value, bool):  # tune files written before this was a choice
            return "bayer" if value else "none"
        if value in DITHERS:
            return value
        raise ValueError(f"dither must be one of {DITHERS}")
    if key == "sharp":
        if value in SCALERS:
            return value
        raise ValueError(f"sharp must be one of {SCALERS}")
    if key == "speed":
        if value == "auto":
            return "auto"
        try:
            return max(0.1, min(1.0, float(value)))
        except (TypeError, ValueError):
            raise ValueError("speed must be 'auto' or a number from 0.1 to 1.0")
    raise ValueError(f"unknown setting {key!r}")


def _gamma_lut(gamma: float):
    """x^gamma, so a value below 1.0 brightens midtones."""
    ramp = np.arange(256, dtype=np.float32) / 255.0
    return np.clip(ramp ** gamma * 255.0 + 0.5, 0, 255).astype(np.uint8)


def eq_filter(gamma: float, saturation: float):
    """ffmpeg's eq stage for the video path, or None when it is a no-op.

    ffmpeg's eq computes x^(1/gamma) while pack() computes x^gamma, so the
    value MUST be inverted here. Passing it through unchanged would darken
    video by exactly the amount it brightened stills.
    """
    if abs(gamma - 1.0) < 1e-3 and abs(saturation - 1.0) < 1e-3:
        return None
    return f"eq=gamma={1.0 / gamma:.4f}:saturation={saturation:.3f}"


def load_tune() -> None:
    try:
        stored = json.loads(TUNE_FILE.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(stored, dict):
        return
    for key, default in TUNE_DEFAULTS.items():
        try:
            TUNE[key] = coerce_tune(key, stored[key]) if key in stored else default
        except ValueError:
            TUNE[key] = default


def save_tune() -> None:
    try:
        MEDIA.mkdir(parents=True, exist_ok=True)
        tmp = TUNE_FILE.with_name(TUNE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(TUNE))
        tmp.replace(TUNE_FILE)
    except OSError as exc:
        print(f"[tune not saved] {exc}", flush=True)

IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
VID_EXT = {".gif", ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
ALL_EXT = IMG_EXT | VID_EXT


# --------------------------------------------------------------------------
# pixels
# --------------------------------------------------------------------------

def pack(img: Image.Image) -> bytes:
    """RGB image sized exactly WxH -> RGB565 little-endian bytes.

    Quantises by rounding, not by shifting. `r >> 3` truncates, which darkens
    every channel by up to 7/255 and shifts hue; scaling to the 5/6/5 range
    with rounding halves the average error for the same cost.
    """
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    gamma = TUNE.get("gamma", 1.0)
    if abs(gamma - 1.0) > 1e-3:
        arr = _gamma_lut(gamma)[arr]

    channels = arr.astype(np.uint32)
    r, g, b = channels[..., 0], channels[..., 1], channels[..., 2]
    if BGR:
        r, b = b, r

    r5 = (r * 31 + 127) // 255
    g6 = (g * 63 + 127) // 255
    b5 = (b * 31 + 127) // 255
    return ((r5 << 11) | (g6 << 5) | b5).astype("<u2").tobytes()


FIT_MODES = ("contain", "cover", "pixel")


def pixel_target(w: int, h: int) -> tuple[int, int]:
    """Integer-scaled size for nearest-neighbour (pixel-art) rendering.

    Shared by the PIL and ffmpeg paths so they cannot disagree. Sources at or
    below panel size scale up by a whole factor; larger ones scale down by a
    whole divisor, where rounding to 1 keeps a near-panel-size source at a
    pixel-exact 1:1 and lets the edges be cropped instead of resampled.
    """
    if w <= W and h <= H:
        factor = max(1, min(W // w, H // h))
        return w * factor, h * factor
    divisor = max(1, round(max(w / W, h / H)))
    return max(1, round(w / divisor)), max(1, round(h / divisor))


def fit(img: Image.Image, mode: str = "contain") -> Image.Image:
    """Scale to exactly WxH, preserving aspect ratio.

    contain: fits entirely inside, centred on black (nothing is lost).
    cover:   fills the panel, centre-cropping the overflow.
    pixel:   nearest-neighbour at whole-number scale, so pixel art stays crisp.
    """
    img = img.convert("RGB")

    if mode == "pixel":
        size = pixel_target(img.width, img.height)
        scaled = img.resize(size, Image.NEAREST)
        canvas = Image.new("RGB", (W, H), "black")
        # A negative offset makes paste clip, so this one call both letterboxes
        # a smaller result and centre-crops a larger one.
        canvas.paste(scaled, ((W - size[0]) // 2, (H - size[1]) // 2))
        return canvas

    if mode == "cover":
        # Pick the source region with the panel's aspect ratio and resize that
        # directly. Scaling up first and cropping after would allocate a huge
        # intermediate for extreme aspect ratios (7x900 -> 480x61714).
        aspect = W / H
        if img.width / img.height > aspect:
            crop_w, crop_h = round(img.height * aspect), img.height
        else:
            crop_w, crop_h = img.width, round(img.width / aspect)
        crop_w = max(1, min(crop_w, img.width))
        crop_h = max(1, min(crop_h, img.height))
        left, top = (img.width - crop_w) // 2, (img.height - crop_h) // 2
        return img.resize((W, H), Image.LANCZOS,
                          box=(left, top, left + crop_w, top + crop_h))

    scale = min(W / img.width, H / img.height)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    img = img.resize(size, Image.LANCZOS)
    canvas = Image.new("RGB", (W, H), "black")
    canvas.paste(img, ((W - size[0]) // 2, (H - size[1]) // 2))
    return canvas


_FONTS: dict[int, object] = {}
_FONT_FILES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # the container
    "/Library/Fonts/Arial.ttf",                         # a mac running the tests
    "/System/Library/Fonts/Helvetica.ttc",
)


def font(size: int):
    """A scalable font. Must be TrueType: the bitmap fallback rejects anchors."""
    if size not in _FONTS:
        for path in _FONT_FILES:
            if os.path.exists(path):
                _FONTS[size] = ImageFont.truetype(path, size)
                break
        else:
            try:
                _FONTS[size] = ImageFont.load_default(size)
            except TypeError:  # Pillow < 10.1 has no sized default
                _FONTS[size] = ImageFont.load_default()
    return _FONTS[size]


def colour(value, fallback: str) -> str:
    """Accept only #rrggbb, so user input can't raise inside PIL."""
    text = str(value or "")
    return text if re.fullmatch(r"#[0-9a-fA-F]{6}", text) else fallback


def as_int(value, default: int, lo: int, hi: int) -> int:
    """Clamp a JSON field to a range; junk falls back instead of 500ing."""
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def wrap(draw, text: str, fnt, width: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        for word in words[1:]:
            trial = f"{cur} {word}"
            if draw.textlength(trial, font=fnt) <= width:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def render_text(text: str, size: int = 34, fg: str = "#ffffff", bg: str = "#000000") -> bytes:
    img = Image.new("RGB", (W, H), colour(bg, "#000000"))
    draw = ImageDraw.Draw(img)
    fnt = font(size)
    lines = wrap(draw, text or "", fnt, W - 24)
    line_h = round(size * 1.25)
    y = max(2, (H - line_h * len(lines)) // 2)
    for line in lines:
        draw.text(((W - draw.textlength(line, font=fnt)) / 2, y),
                  line, font=fnt, fill=colour(fg, "#ffffff"))
        y += line_h
    return pack(img)


def diag_pattern() -> bytes:
    """One static image that separates banding, colour error and softness.

    Top: smooth ramps - any visible steps are 16-bit banding.
    Middle: flat reference patches - judge colour accuracy here, not on gradients.
    Bottom: 1px checks and lines - if these look grey or smeared rather than
    crisp, the softness is in scaling, not the panel's motion response.
    """
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)

    ramps = (("grey", (1, 1, 1)), ("red", (1, 0, 0)),
             ("green", (0, 1, 0)), ("blue", (0, 0, 1)))
    for row, (label, mask) in enumerate(ramps):
        y0 = row * 36
        for x in range(W):
            level = round(x * 255 / (W - 1))
            draw.line([(x, y0), (x, y0 + 35)],
                      fill=tuple(level * m for m in mask))
        draw.text((4, y0 + 2), label, font=font(13), fill="#ffffff" if row else "#ff2020")

    patches = (((255, 0, 0), "R"), ((0, 255, 0), "G"), ((0, 0, 255), "B"),
               ((255, 255, 255), "W"), ((128, 128, 128), "50"),
               ((255, 200, 160), "skin"), ((0, 0, 0), "K"), ((255, 255, 0), "Y"))
    pw = W // len(patches)
    for i, (rgb, label) in enumerate(patches):
        draw.rectangle([i * pw, 144, (i + 1) * pw - 1, 214], fill=rgb)
        draw.text((i * pw + 4, 196), label, font=font(12),
                  fill="#000000" if sum(rgb) > 380 else "#ffffff")

    for x in range(0, W // 2, 2):          # 1px vertical lines
        draw.line([(x, 220), (x, 268)], fill="#ffffff")
    for y in range(220, 269):              # 1px checkerboard
        for x in range(W // 2, W, 2):
            draw.point((x + (y % 2), y), fill="#ffffff")

    draw.text((W / 2, 300), "ramps: banding   patches: colour   fine: sharpness",
              font=font(15), fill="#c9d6e2", anchor="ms")
    return pack(img)


def test_pattern() -> bytes:
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    # Explicit tuples, not colour names: CSS "green" is #008000, so naming it
    # would render the middle bar at half scale and defeat the whole point.
    for i, rgb in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255))):
        draw.rectangle([i * W // 3, 0, (i + 1) * W // 3, H], fill=rgb)
    draw.text((W / 2, H - 22), "R    G    B", font=font(22), fill="white", anchor="ms")
    return pack(img)


# --------------------------------------------------------------------------
# system stats  (/proc values are the host's even inside a container)
# --------------------------------------------------------------------------

def _cpu_sample() -> tuple[int, int]:
    fields = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle


_cpu_prev = _cpu_sample() if os.path.exists("/proc/stat") else (0, 0)


def stats() -> dict:
    global _cpu_prev
    total, idle = _cpu_sample()
    d_total, d_idle = total - _cpu_prev[0], idle - _cpu_prev[1]
    cpu = 0.0
    if d_total > 0:
        _cpu_prev = (total, idle)
        cpu = 100.0 * (d_total - d_idle) / d_total

    mem: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, val = line.partition(":")
        mem[key] = int(val.split()[0])  # kB
    mem_total = mem.get("MemTotal", 1)
    mem_avail = mem.get("MemAvailable", mem.get("MemFree", 0))

    temp = None
    zone = Path("/sys/class/thermal/thermal_zone0/temp")
    if zone.exists():
        try:
            temp = int(zone.read_text().strip()) / 1000.0
        except (ValueError, OSError):
            temp = None

    usage = shutil.disk_usage(MEDIA if MEDIA.is_dir() else Path("/"))
    return {
        "cpu": cpu,
        "mem_pct": 100.0 * (mem_total - mem_avail) / mem_total,
        "mem_used_mb": (mem_total - mem_avail) / 1024,
        "mem_total_mb": mem_total / 1024,
        "temp": temp,
        "load": Path("/proc/loadavg").read_text().split()[:3],
        "uptime": float(Path("/proc/uptime").read_text().split()[0]),
        "disk_pct": 100.0 * usage.used / usage.total,
        "disk_free_gb": usage.free / 1e9,
    }


def _bar(draw, x, y, w, h, pct, fill):
    draw.rounded_rectangle([x, y, x + w, y + h], 3, fill="#1b2430")
    width = max(0.0, min(100.0, pct)) / 100 * w
    if width >= 2:
        draw.rounded_rectangle([x, y, x + width, y + h], 3, fill=fill)


def render_sysmon() -> bytes:
    s = stats()
    img = Image.new("RGB", (W, H), "#0b0f14")
    draw = ImageDraw.Draw(img)

    draw.text((16, 12), "SYSTEM", font=font(18), fill="#4c6178")
    up = int(s["uptime"])
    draw.text((W - 16, 12), f"up {up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m",
              font=font(16), fill="#4c6178", anchor="ra")

    rows = [
        ("CPU", s["cpu"], f"{s['cpu']:.0f}%", "#3fa7ff"),
        ("MEM", s["mem_pct"], f"{s['mem_used_mb']:.0f} / {s['mem_total_mb']:.0f} MB", "#4be08b"),
        ("DISK", s["disk_pct"], f"{s['disk_free_gb']:.1f} GB free", "#c9a227"),
    ]
    if s["temp"] is not None:
        # 85C is the SoC throttle ceiling, so scale the bar against that.
        rows.append(("TEMP", s["temp"] / 85 * 100, f"{s['temp']:.1f}°C", "#ff6b5b"))

    y = 56
    for label, pct, text, fill in rows:
        draw.text((16, y), label, font=font(17), fill="#8fa3b8")
        draw.text((W - 16, y), text, font=font(17), fill="#e6edf3", anchor="ra")
        _bar(draw, 16, y + 24, W - 32, 14, pct, fill)
        y += 58

    draw.text((16, H - 26), "load  " + "  ".join(s["load"]), font=font(16), fill="#4c6178")
    return pack(img)


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

def media_path(name) -> Path:
    """Resolve a user-supplied name strictly inside MEDIA."""
    safe = secure_filename(name or "")
    if not safe:
        raise ValueError("bad filename")
    path = (MEDIA / safe).resolve()
    if path.parent != MEDIA.resolve() or not path.is_file():
        raise ValueError("not found")
    return path


def upload_name(original: str):
    """A safe filename that keeps the extension. None if the type is unsupported.

    secure_filename strips non-ASCII, so a wholly non-Latin name would otherwise
    sanitise down to nothing and get rejected for no visible reason.
    """
    ext = Path(original or "").suffix.lower()
    if ext not in ALL_EXT:
        return None
    safe = secure_filename(original or "")
    if Path(safe).stem and Path(safe).suffix.lower() == ext:
        return safe
    return f"{Path(safe).stem or f'upload-{int(time.time())}'}{ext}"


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".gif":
        try:
            with Image.open(path) as im:
                return "video" if getattr(im, "n_frames", 1) > 1 else "image"
        except Exception:
            return "image"
    if ext in VID_EXT:
        return "video"
    return "image" if ext in IMG_EXT else "unknown"


def load_fits() -> dict:
    """Per-file fit preferences. Unknown or corrupt entries are ignored."""
    try:
        data = json.loads(FITS.read_text())
        return {str(k): v for k, v in data.items() if v in FIT_MODES}
    except (OSError, ValueError, AttributeError):
        return {}


def set_fit(name: str, mode: str) -> None:
    fits = load_fits()
    fits[name] = mode
    try:
        tmp = FITS.with_name(FITS.name + ".tmp")
        tmp.write_text(json.dumps(fits))
        tmp.replace(FITS)
    except OSError as exc:
        print(f"[fit not saved] {exc}", flush=True)


def fit_for(name: str) -> str:
    return load_fits().get(name, "contain")


def library() -> list[dict]:
    if not MEDIA.is_dir():
        return []
    fits = load_fits()
    return [
        {"name": p.name, "kind": kind_of(p), "size": p.stat().st_size,
         "fit": fits.get(p.name, "contain")}
        for p in sorted(MEDIA.iterdir(), key=lambda p: p.name.lower())
        if p.is_file() and p.suffix.lower() in ALL_EXT
    ]


# --------------------------------------------------------------------------
# sources: iterables of frames
# --------------------------------------------------------------------------

def once(frame: bytes):
    """A still frame. The framebuffer retains it, so there is nothing to loop."""
    yield frame


def sysmon_source(screen: "Screen", interval: float = 2.0):
    while True:
        yield render_sysmon()
        screen.nap(interval)


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def probe_stream(path: Path):
    """(width, height, native_fps) of the first video stream, or None."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,avg_frame_rate",
             "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        w, h, rate = out.split("x")[:3]
        num, _, den = rate.partition("/")
        native = float(num) / float(den or 1)
        return int(w), int(h), (native if native > 0 else None)
    except (OSError, ValueError, ZeroDivisionError, subprocess.SubprocessError):
        return None


def playback_speed(native: float | None, target: int) -> float:
    """How much to slow a clip so no frame has to be dropped.

    A 25 fps GIF on a 13 fps panel is otherwise resampled by throwing half the
    frames away: correct timing, half the animation, visibly choppy. Slowing the
    timeline instead shows every frame. Clips already within the ceiling are
    untouched.
    """
    if not native or native <= target:
        return 1.0
    return max(0.1, min(1.0, target / native))


def video_source(path: Path, fps: int = MAX_FPS, loop: bool = True, mode: str = "contain"):
    """ffmpeg emits frames already in the framebuffer's exact pixel format."""
    # Pixel art must never be interpolated; everything else gets the chosen
    # scaler. ffmpeg's default is bicubic, which is noticeably soft.
    scaler = "neighbor" if mode == "pixel" else TUNE.get("sharp", "lanczos")
    if scaler not in SCALERS:
        scaler = "lanczos"

    info = probe_stream(path)
    if mode == "pixel" and info:
        nw, nh = pixel_target(info[0], info[1])
        # crop then pad handles both directions: crop is a no-op when the scaled
        # result is already smaller, pad a no-op when it was larger.
        geom = (f"scale={nw}:{nh}:flags={scaler},"
                rf"crop=min(iw\,{W}):min(ih\,{H}),"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black")
    elif mode == "cover":
        geom = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags={scaler},"
                f"crop={W}:{H}")
    else:
        geom = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags={scaler},"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black")
    wanted = TUNE.get("speed", "auto")
    if wanted == "auto":
        speed = playback_speed(info[2] if info else None, fps)
    else:
        speed = max(0.1, min(1.0, float(wanted)))

    stages = []
    if speed < 0.999:
        # Stretch the timeline before resampling, so fps= has every frame to keep.
        stages.append(f"setpts=PTS/{speed:.4f}")
        print(f"[speed] {path.name}: native {info[2] if info else '?'} fps -> "
              f"{speed:.2f}x so all frames survive at {fps} fps", flush=True)
    stages += [f"fps={fps}", geom]

    eq = eq_filter(TUNE.get("gamma", 1.0), TUNE.get("saturation", 1.0))
    if eq:
        stages.append(eq)
    vf = ",".join(stages)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if loop:
        cmd += ["-stream_loop", "-1"]  # loop inside ffmpeg: no per-loop respawn hitch
    cmd += ["-i", str(path), "-vf", vf]
    # Dropping 24-bit colour to the panel's 16-bit bands badly on flat areas and
    # gradients; dithering trades that for fine noise, which reads far cleaner.
    dither = TUNE.get("dither", "bayer")
    cmd += ["-sws_dither", dither if dither in DITHERS else "bayer",
            "-sws_flags", "accurate_rnd+full_chroma_int"]
    cmd += ["-f", "rawvideo", "-pix_fmt", "bgr565le" if BGR else "rgb565le", "-"]

    errlog = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errlog)
    shown = 0
    try:
        period = 1.0 / max(1, fps)
        due = time.monotonic()
        while True:
            frame = _read_exact(proc.stdout, FRAME)
            if len(frame) < FRAME:
                break
            due += period
            slack = due - time.monotonic()
            if slack > 0:
                time.sleep(slack)
            else:
                due = time.monotonic()  # panel can't keep up; don't accrue debt
            shown += 1
            yield frame
        if shown == 0:
            errlog.seek(0)
            detail = errlog.read().decode(errors="replace").strip()
            raise RuntimeError(detail[-400:] or "ffmpeg produced no frames")
    finally:
        proc.kill()
        proc.wait()
        errlog.close()


def slideshow_source(screen: "Screen", names, seconds: float, fps: int, shuffle: bool):
    """Cycle the library forever.

    Delegates to the single-item sources rather than reimplementing them: a
    still holds for `seconds`, a clip plays (looping) for `seconds` then yields
    to the next item.
    """
    while True:
        order = list(names)
        if shuffle:
            random.shuffle(order)

        shown = 0
        for name in order:
            try:
                path = media_path(name)  # re-resolved: it may have been deleted
            except ValueError:
                continue
            shown += 1
            screen.detail = path.name

            mode = fit_for(path.name)  # each item keeps its own framing
            if kind_of(path) == "video":
                clip = video_source(path, fps, loop=True, mode=mode)
                try:
                    deadline = time.monotonic() + seconds
                    for frame in clip:
                        yield frame
                        if time.monotonic() >= deadline:
                            break
                finally:
                    clip.close()  # runs video_source's finally, killing ffmpeg
            else:
                with Image.open(path) as im:
                    yield pack(fit(im, mode))
                screen.nap(seconds)

        if shown == 0:
            yield render_text("slideshow\nnothing to show", 28, "#ff6b5b", "#0b0f14")
            return


# --------------------------------------------------------------------------
# the painter
# --------------------------------------------------------------------------

class Screen:
    def __init__(self):
        self._cv = threading.Condition()
        self._gen = None
        self._epoch = 0
        self._wake = threading.Event()
        self._times: deque[float] = deque(maxlen=30)
        self.status: dict = {"mode": "idle"}
        # A running source may annotate itself here (the slideshow reports the
        # item it is on). The painter clears it when picking up a new source,
        # so a stale note cannot outlive the source that wrote it.
        self.detail: str | None = None

    def show(self, gen, status: dict) -> None:
        with self._cv:
            self._gen = gen
            self._epoch += 1
            self.status = status
            self._wake.set()
            self._cv.notify_all()

    def nap(self, seconds: float) -> None:
        """Sleep inside a source, but return at once if superseded."""
        self._wake.wait(seconds)

    @property
    def fps(self) -> float:
        if len(self._times) < 2:
            return 0.0
        span = self._times[-1] - self._times[0]
        return (len(self._times) - 1) / span if span > 0 else 0.0

    def blit(self, frame: bytes) -> None:
        if len(frame) != FRAME:
            raise ValueError(f"frame is {len(frame)} bytes, expected {FRAME}")
        # ponytail: reopened per frame. A 307KB SPI write dwarfs open(), and this
        # survives the panel being re-probed instead of holding a stale fd.
        with open(FB, "wb") as fb:
            fb.write(frame)
        self._times.append(time.monotonic())

    def run(self) -> None:
        while True:
            with self._cv:
                while self._gen is None:
                    self._cv.wait()
                gen, epoch = self._gen, self._epoch
                self._wake.clear()
                self._times.clear()
                self.detail = None
            try:
                for frame in gen:
                    if self._epoch != epoch:  # superseded by a newer source
                        break
                    self.blit(frame)
            except Exception as exc:  # a bad file must not kill the painter
                print(f"[source failed] {exc}", flush=True)
                with self._cv:
                    if self._epoch == epoch:
                        self.status = {"mode": "error", "detail": str(exc)[:300]}
            finally:
                gen.close()
            with self._cv:
                if self._epoch == epoch:
                    self._gen = None


screen = Screen()


# --------------------------------------------------------------------------
# specs: a serialisable description of what belongs on the panel
# --------------------------------------------------------------------------

def apply_spec(spec: dict, remember: bool = True) -> dict:
    """Turn a spec into a live source, and return the canonical form.

    One dispatcher serves both the API and the boot-time restore, so what comes
    back after a reboot cannot drift from what the API would have produced.
    Raises ValueError on anything unusable.
    """
    mode = spec.get("mode")

    if mode in ("image", "video"):
        path = media_path(spec.get("name"))
        kind = kind_of(path)
        if kind == "unknown":
            raise ValueError("unsupported file type")
        # An explicit fit becomes this file's remembered preference.
        asked = spec.get("fit")
        if asked in FIT_MODES:
            set_fit(path.name, asked)
        how = asked if asked in FIT_MODES else fit_for(path.name)

        if kind == "video":
            canon = {
                "mode": "video",
                "name": path.name,
                "fps": as_int(spec.get("fps"), MAX_FPS, 1, MAX_FPS),
                "loop": bool(spec.get("loop", True)),
                "fit": how,
            }
            screen.show(video_source(path, canon["fps"], canon["loop"], how), dict(canon))
        else:
            canon = {"mode": "image", "name": path.name, "fit": how}
            with Image.open(path) as im:
                screen.show(once(pack(fit(im, how))), dict(canon))

    elif mode == "text":
        canon = {
            "mode": "text",
            "text": str(spec.get("text", ""))[:2000],
            "size": as_int(spec.get("size"), 34, 10, 96),
            "fg": colour(spec.get("fg"), "#ffffff"),
            "bg": colour(spec.get("bg"), "#000000"),
        }
        screen.show(
            once(render_text(canon["text"], canon["size"], canon["fg"], canon["bg"])),
            dict(canon),
        )

    elif mode == "slideshow":
        names = spec.get("names") or [m["name"] for m in library()]
        if not names:
            raise ValueError("nothing in the library to cycle")
        canon = {
            "mode": "slideshow",
            "names": [str(n) for n in names],
            "seconds": as_int(spec.get("seconds"), 8, 1, 3600),
            "fps": as_int(spec.get("fps"), MAX_FPS, 1, MAX_FPS),
            "shuffle": bool(spec.get("shuffle", False)),
        }
        screen.show(
            slideshow_source(screen, canon["names"], canon["seconds"],
                             canon["fps"], canon["shuffle"]),
            dict(canon) | {"count": len(canon["names"])},
        )

    elif mode == "sysmon":
        canon = {"mode": "sysmon"}
        screen.show(sysmon_source(screen), dict(canon))

    elif mode == "test":
        canon = {"mode": "test"}
        screen.show(once(test_pattern()), dict(canon))

    elif mode == "diag":
        canon = {"mode": "diag"}
        screen.show(once(diag_pattern()), dict(canon))

    elif mode == "clear":
        canon = {"mode": "clear"}
        screen.show(once(b"\x00" * FRAME), dict(canon))

    else:
        raise ValueError(f"unknown mode {mode!r}")

    if remember:
        save_spec(canon)
    return canon


def save_spec(spec: dict) -> None:
    """Write via a temp file and rename: a power cut can't leave broken JSON."""
    try:
        MEDIA.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_name(STATE.name + ".tmp")
        tmp.write_text(json.dumps(spec))
        tmp.replace(STATE)
    except OSError as exc:
        print(f"[state not saved] {exc}", flush=True)


def load_spec():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return None


def disarm_guard() -> None:
    """The restore has held up long enough to be trusted; re-arm for next boot."""
    try:
        GUARD.unlink(missing_ok=True)
    except OSError:
        pass


def restore_saved_spec() -> bool:
    """Replay the persisted spec, refusing to repeat one that killed us.

    The panel can be driven hard enough to destabilise the Pi. Without this,
    persisting "play at 30 fps" would faithfully reproduce the crash on every
    boot, forever. One unexplained loss of the device disarms the restore.
    """
    saved = load_spec()
    if not saved:
        return False

    if GUARD.exists():
        print("[restore skipped] the previous boot did not survive its restore; "
              "showing the splash instead", flush=True)
        disarm_guard()
        return False

    try:
        GUARD.touch()
    except OSError:
        pass  # read-only volume: restore anyway, just without the safety net

    try:
        apply_spec(saved, remember=False)
    except Exception as exc:
        # A clean rejection (missing file, bad spec) is not a crash.
        print(f"[restore failed] {exc}", flush=True)
        disarm_guard()
        return False

    timer = threading.Timer(GUARD_HOLD, disarm_guard)
    timer.daemon = True
    timer.start()
    print(f"[restored] {saved.get('mode')}", flush=True)
    return True


# --------------------------------------------------------------------------
# web
# --------------------------------------------------------------------------

app = Flask(__name__, static_folder=None)
# Bound so a stray huge upload can't quietly fill the SD card.
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", 512)) * 1024 * 1024

AUTH_USER = os.environ.get("AUTH_USER", "admin")
AUTH_PASS = os.environ.get("AUTH_PASS", "")  # empty disables auth entirely


@app.before_request
def require_auth():
    if not AUTH_PASS:
        return None
    given = request.authorization
    ok = (
        given is not None
        and given.type == "basic"
        and secrets.compare_digest(given.username or "", AUTH_USER)
        and secrets.compare_digest(given.password or "", AUTH_PASS)
    )
    if ok:
        return None
    return Response("authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="pidisplay"'})


@app.get("/")
def index():
    return send_file(Path(__file__).with_name("index.html"))


@app.get("/api/state")
def api_state():
    return jsonify(status=screen.status, fps=round(screen.fps, 1),
                   detail=screen.detail, media=library(), stats=stats(),
                   panel_fps=round(PANEL_FPS, 1), max_fps=MAX_FPS,
                   spi_hz=_spi_hz(), spi_actual=actual_spi_hz(_spi_hz()), tune=TUNE)


@app.post("/api/upload")
def api_upload():
    saved, rejected = [], []
    MEDIA.mkdir(parents=True, exist_ok=True)
    for f in request.files.getlist("files"):
        name = upload_name(f.filename or "")
        if name is None:
            rejected.append(f.filename)
            continue
        f.save(MEDIA / name)
        saved.append(name)
    return jsonify(saved=saved, rejected=rejected)


@app.post("/api/show")
def api_show():
    """The only route that changes the panel. Body is a spec; see apply_spec."""
    try:
        canon = apply_spec(request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, spec=canon)


@app.post("/api/tune")
def api_tune():
    """Adjust picture settings and re-apply what is on screen immediately.

    A 16-bit panel with an unknown gamma curve can only be tuned by looking at
    it, so this exists to make A/B comparison instant rather than a redeploy.
    """
    body = request.get_json(silent=True) or {}
    if body.get("reset"):
        changed = dict(TUNE_DEFAULTS)
    else:
        changed = {}
        for key in TUNE_DEFAULTS:
            if key in body:
                try:
                    changed[key] = coerce_tune(key, body[key])
                except ValueError as exc:
                    return jsonify(error=str(exc)), 400

    if not changed:
        return jsonify(error="nothing to change"), 400

    TUNE.update(changed)
    save_tune()
    # Re-render whatever is showing so the change is visible at once.
    live = dict(screen.status)
    if live.get("mode") in ("image", "video", "slideshow", "text", "diag", "test"):
        try:
            apply_spec(live, remember=False)
        except ValueError:
            pass
    return jsonify(ok=True, tune=TUNE)


@app.post("/api/fit")
def api_fit():
    """Remember a file's framing. Re-applies at once if it is on screen now."""
    body = request.get_json(silent=True) or {}
    mode = body.get("fit")
    if mode not in FIT_MODES:
        return jsonify(error=f"fit must be one of {FIT_MODES}"), 400
    try:
        path = media_path(body.get("name"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    set_fit(path.name, mode)
    live = screen.status
    if live.get("mode") in ("image", "video") and live.get("name") == path.name:
        apply_spec({**live, "fit": mode})
    return jsonify(ok=True, name=path.name, fit=mode)


@app.delete("/api/media/<name>")
def api_delete(name):
    try:
        media_path(name).unlink()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True)


@app.get("/api/thumb/<name>")
def api_thumb(name):
    try:
        path = media_path(name)
    except ValueError as exc:
        return jsonify(error=str(exc)), 404

    buf = io.BytesIO()
    try:
        with Image.open(path) as im:
            thumb = im.convert("RGB")
            thumb.thumbnail((200, 200), Image.BILINEAR)
            thumb.save(buf, "JPEG", quality=70)
        data = buf.getvalue()
    except Exception:
        # Not readable by PIL (mp4 and friends) - let ffmpeg grab one frame.
        try:
            out = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
                 "-frames:v", "1", "-vf", "scale=200:-1", "-f", "image2",
                 "-c:v", "mjpeg", "-"],
                capture_output=True, timeout=25)
        except subprocess.TimeoutExpired:
            return Response(status=204)
        if out.returncode != 0 or not out.stdout:
            return Response(status=204)
        data = out.stdout

    return Response(data, mimetype="image/jpeg",
                    headers={"Cache-Control": "max-age=86400"})


def main():
    MEDIA.mkdir(parents=True, exist_ok=True)
    load_tune()
    requested, real = _spi_hz(), actual_spi_hz(_spi_hz())
    print(f"[panel] SPI requested {(requested or 0)/1e6:.0f} MHz -> actual "
          f"{(real or 0)/1e6:.2f} MHz (core {CORE_HZ/1e6:.0f}) -> ceiling "
          f"{PANEL_FPS:.1f} fps, cap {MAX_FPS} | tune {TUNE}", flush=True)
    threading.Thread(target=screen.run, daemon=True, name="painter").start()

    # Put the last thing back on the panel before serving anything. A bad or
    # stale spec must never stop the web UI from coming up.
    if not restore_saved_spec():
        hint = os.environ.get("HOST_HINT", "port 8080")
        screen.show(once(render_text(f"pidisplay\n{hint}", 30, "#9fb3c8", "#0b0f14")),
                    {"mode": "text"})
    # ponytail: Flask's own server. It is one LAN client on a 480x320 panel;
    # swap in waitress only if that ever stops being true.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)


if __name__ == "__main__":
    main()
