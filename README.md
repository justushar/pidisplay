# pidisplay

Web-controlled display server for a 3.5" ILI9486 SPI panel on a Raspberry Pi
Zero 2 W. Shows images, GIFs/video, text cards, or a live system monitor.
Runs as a rootless podman container, starts on boot.

## How it works

Everything on the panel is **one 307,200-byte RGB565 frame written to
`/dev/fb1`** (480 x 320 x 2 bytes; stride is exactly 960, so rows have no
padding). That single fact is what keeps this small:

```
web UI (your phone/laptop) --> Flask --> sets the active source
                                             |
                    painter thread: for frame in source: write(frame)
                                             |
        +--------------+--------------+-------------+------------+
      image           video          text         sysmon
   (PIL -> 565)   (ffmpeg 565)   (PIL -> 565)  (PIL -> 565)
```

A **source is just an iterable of frames**, so switching content means swapping
one object — the painter breaks its loop and picks up the new one. There is no
per-type teardown logic.

Consequences worth knowing:

- **Stills yield exactly one frame and stop.** The framebuffer retains what was
  last written, so holding an image costs nothing — no refresh loop.
- **Video never touches an image library.** `ffmpeg -pix_fmt rgb565le` emits
  bytes already in the panel's exact format, so the video path is
  `read(307200) -> write()`. `-stream_loop -1` makes GIFs loop inside ffmpeg,
  which avoids a visible hitch every time a short clip restarts.
- **The slideshow delegates.** It does not reimplement playback — it calls the
  same image and video sources, holding a still for the interval and letting a
  clip loop for the interval before advancing.

## What is on screen, and why it comes back

Everything displayable is described by a small JSON **spec**:

```json
{"mode": "image",     "name": "cat.png", "fit": "contain"}
{"mode": "video",     "name": "loop.gif", "fps": 10, "loop": true, "fit": "cover"}
{"mode": "text",      "text": "hello", "size": 34, "fg": "#ffffff", "bg": "#000000"}
{"mode": "slideshow", "names": ["a.gif", "b.png"], "seconds": 8, "fps": 10, "shuffle": false}
{"mode": "sysmon"}
{"mode": "test"}
{"mode": "clear"}
```

`POST /api/show` with one of these is **the only route that changes the panel** —
all seven modes go through a single dispatcher (`apply_spec`). Each accepted
spec is normalised (values clamped, colours validated, names resolved) and
written to `~/pidisplay/media/.state.json` via temp-file-and-rename, so a power
cut cannot leave broken JSON.

On startup the service reads that file and replays it through the *same*
dispatcher, which is why a restored panel cannot drift from what the API would
have produced. If the spec is stale — say the image was deleted — it logs the
failure, falls back to a splash, and still serves the web UI.

The unit is deliberately **not** ordered after `network-online.target`: painting
the panel needs no network, and waiting for DHCP would delay the restored image
by 10–30 s. The UI just becomes reachable a moment later.

> A `@reboot` cron entry would also start it, but the quadlet is strictly
> better here: it restarts on failure, and it cannot race podman's own startup
> the way cron can.

## Deploy

One-time, on the Pi:

```sh
sudo apt update && sudo apt install -y podman
```

Then from this directory, any time:

```sh
./deploy.sh
```

That copies the source, builds the image, **runs the self-checks inside the
image**, installs the systemd user unit, and restarts the service. It prints the
web password at the end.

`deploy.sh` uses **SSH key auth by default**. To use a password instead, or to
point at a different host:

```sh
PI_PASS=... ./deploy.sh
PI_HOST=pi@192.168.1.50 ./deploy.sh
```

To have those remembered, copy `.deploy.env.example` to `.deploy.env` and edit
it — that file is gitignored and never committed.

Then open **http://&lt;pi-address&gt;:8080** with the user and password printed at
the end of the deploy.

> No credentials live in this repo. The web password is generated on first
> deploy into `~/pidisplay/auth.env` on the Pi (mode 0600) and survives
> redeploys; delete that file and redeploy to rotate it.

## Files

| File | Purpose |
|---|---|
| `display.py` | Framebuffer writer, the four sources, Flask API |
| `index.html` | The whole UI. No build step, no framework |
| `Containerfile` | `debian:trixie-slim` + Debian's prebuilt arm64 packages |
| `pidisplay.container` | podman quadlet — boot autostart, device + volume wiring |
| `deploy.sh` | copy, build, test, restart |
| `test_display.py` | Self-checks for the pure logic |

## Framing: FIT / FILL / PIXEL

The panel is landscape 3:2, but plenty of content is square, portrait, or pixel
art. Each file carries its own framing choice, cycled by the button on its
library card:

- **FIT** (`contain`, the default) — scales to fit entirely inside the panel,
  centred on black. Nothing is lost, but a tall GIF leaves wide side bars.
- **FILL** (`cover`) — fills the panel edge to edge and centre-crops the
  overflow. No bars, but edges are cut, which on a tall image can remove a
  character's head. Hence per-file, not global.
- **PIXEL** (`pixel`) — nearest-neighbour at whole-number scale. For pixel art,
  Lanczos is actively wrong: it interpolates between neighbouring pixels and
  turns crisp edges into gradients. This keeps them hard.

Choices live in `~/pidisplay/media/.fits.json` and persist across reboots. The
slideshow honours each item's setting as it goes. `POST /api/fit` with
`{"name": ..., "fit": "pixel"}` sets it, and re-applies immediately if that item
is the one currently on screen.

### Why PIXEL scales the way it does

`pixel_target()` is shared by the PIL and ffmpeg paths so the two cannot
disagree. Sources at or below panel size scale **up** by a whole factor; larger
ones scale **down** by a whole divisor, rounded — so a source just over panel
size (498x331) stays at a pixel-exact 1:1 and has its edges cropped, rather than
being resampled by 0.96x for a 4% size change.

One `paste` with a possibly-negative offset then handles both directions: it
letterboxes a smaller result and centre-crops a larger one. ffmpeg mirrors this
with `scale=...:flags=neighbor,crop=min(iw\,480):min(ih\,320),pad=480:320`.

The test suite proves the resampling really differs: a hard-edged checkerboard
through PIXEL comes out containing *only* its two original colours, while the
same image through FIT demonstrably blends new ones in.

### Geometry note

The FILL path resizes the chosen source region directly
(`Image.resize(box=...)`) rather than scaling up and cropping afterwards.
Otherwise a 7x900 image would allocate a 480x61714 intermediate — about 88 MB,
which a 415 MB device does not have to spare.

## Operating it

```sh
# on the Pi
export XDG_RUNTIME_DIR=/run/user/$(id -u)      # needed over plain ssh
systemctl --user status  pidisplay.service
systemctl --user restart pidisplay.service
journalctl --user -u pidisplay.service -f
```

- **Uploads** live in `~/pidisplay/media/` on the Pi. Capped at 512 MB per
  request (`MAX_UPLOAD_MB`).
- **Password** lives in `~/pidisplay/auth.env`, mode 0600, and survives
  redeploys. Delete the file and redeploy to rotate it. Setting `AUTH_PASS`
  empty disables auth entirely.

## Tuning the panel

### Framerate, and what the fps number actually means

**Read this before trusting the number in the UI.**

`fbtft` uses deferred I/O: writing to `/dev/fb1` marks pages dirty and returns
at memory speed, while a kernel workqueue pushes pixels over SPI on its own
schedule. So the reported fps measures **how fast frames are handed to the
kernel, not how fast the panel repaints.**

Measured on this device: requesting 15 fps yields exactly 15.0, and requesting
30 fps yields exactly 30.0, at 19 % CPU. Hitting the target *exactly* at both
points is the tell — a saturated bus does not land on round numbers. The
visible refresh rate is whatever the SPI clock and the driver's update cadence
permit, and it is lower than these figures.

Consequences:

- Treat the fps readout as a **throughput and liveness indicator**, not a
  promise about motion. Your eyes on the panel are the real instrument.
- Requesting more fps than the panel can repaint is wasted CPU and bus traffic.
  Start at 10–12 and raise it only if motion visibly improves.
- Sustained high rates are not free. See *Stability* below.

A frame is 307,200 bytes, so the theoretical synchronous ceiling would be
~6 fps at 16 MHz, ~13 at 32 MHz and ~25 at 62 MHz. Those numbers describe the
bus, not what this code measures — do not use them to predict the readout.

### This panel's verified limit

**The requested speed is not the actual speed.** The SPI block divides the core
clock by an *even integer*, so requests snap down to discrete values. Core clock
here is **400 MHz** (`vcgencmd measure_clock core`):

| `speed=` | divisor | actual | ceiling | verdict |
|---|---|---|---|---|
| 16000000 | 26 | 15.38 MHz | 6.3 fps | clean (overlay default) |
| 32000000 | 14 | 28.57 MHz | 11.6 fps | clean |
| **34000000** | **12** | **33.33 MHz** | **13.6 fps** | **clean — current setting** |
| 40000000 / 48000000 | 10 | 40.00 MHz | 16.3 fps | **corrupts** |
| 62000000 | 8 | 50.00 MHz | 20.3 fps | **corrupts** |

**33.33 MHz is this panel's maximum clean clock, and that is a complete answer
rather than the best found so far.** Divisors must be even, so divisor 12
(33.33 MHz, clean) and divisor 10 (40.00 MHz, corrupt) are adjacent — there is
no value between them to try. Do not bother raising `speed` further; the only
remaining lever would be raising `core_freq`, which is overclocking.

Note 40000000 and 48000000 both land on divisor 10 and are therefore identical.

`panel_ceiling()` computes the ceiling from the *actual* clock via
`actual_spi_hz()`. Using the requested figure overstates it by ~12% and quietly
reintroduces the judder the cap exists to prevent.

When the clock is too high the failure signature is *not* tearing or dropped
rows — it is scattered bit errors, which look like:

- **yellow rendering as green** (`0xFFE0` losing its top bit becomes green-dominant)
- **red rendering as dark olive** (`0xF800` shifted down gains a green component)
- white dimming and taking a colour cast
- general fine speckle

If you ever see that combination, suspect the clock, not the software. Confirm
by checking the encoder directly — it should produce exactly `0xF800` / `0x07E0`
/ `0x001F` / `0xFFE0` for red / green / blue / yellow:

```sh
podman exec pidisplay python3 -c "
import display as D; from PIL import Image
b = D.pack(Image.new('RGB', (D.W, D.H), (255,255,0)))
print(hex((b[1] << 8) | b[0]))"   # must be 0xffe0
```

If the bytes are right and the glass is wrong, it is the wire.

The current setting is deliberate, and is the fastest verified-clean value:

```
dtoverlay=tft35a:rotate=90,speed=34000000,fps=30
```

A known-good copy of the original boot config is kept at
`/boot/firmware/config.txt.bak-pidisplay`.

### Fast animation: slow it, don't drop frames

A 25 fps GIF on an 11.6 fps panel cannot show every frame in real time. There
are two ways to resolve that, and the default is the second:

- **real time** — ffmpeg's `fps=` filter discards over half the frames. Correct
  timing, half the animation, visibly choppy.
- **`speed: "auto"` (default)** — `setpts` stretches the timeline by
  `ceiling / native_fps` first, so `fps=` has every frame to keep. Smooth
  motion, played slower. A 25 fps clip becomes 0.44x here; anything already
  within the ceiling is untouched, so this adapts per file with no configuration.

Override globally in the Picture section, or `POST /api/tune {"speed": 0.5}`.

To change the clock, edit `/boot/firmware/config.txt`:

```
dtoverlay=tft35a:rotate=90,speed=32000000,fps=30
```

then reboot. **Raise it one step at a time.** Plenty of ILI9486 boards tear or
show corrupt rows above 32 MHz, and the failure looks like a broken app rather
than a broken setting. It cannot break SSH, so it is always recoverable by
editing the value back down.

### Stability

During testing, the Pi dropped off the network entirely — both port 22 and
8080 — while looping video at 30 fps, and needed a power cycle. Cause was never
proven; wifi is possible, but the timing points at **power**. A Zero 2 W under
sustained CPU + SPI + wifi load draws meaningfully more current, and a marginal
supply or a thin USB cable browns out exactly like that. It was only 41.9 °C, so
thermal throttling was not the issue.

It has since run 30 fps without incident, so this is not a hard limit. But if
the device becomes unreachable under load, suspect the power supply before the
software.

This is also why the **crash-loop breaker** exists (see above). Without it,
persisting "play at 30 fps" would have faithfully reproduced that crash on every
subsequent boot.

### Picture quality

The panel is 16-bit (65k colours) with its own gamma curve, so picture settings
can only be judged by looking at it. `POST /api/tune` changes them and
re-renders what is on screen immediately; they persist in
`~/pidisplay/media/.tune.json`.

| Setting | Default | Effect |
|---|---|---|
| `sharp` | `lanczos` | ffmpeg scaler. ffmpeg's own default is **bicubic**, which is visibly soft. |
| `dither` | `bayer` | How 24-bit colour is reduced to 16-bit. |
| `gamma` | `1.0` | <1 brightens midtones, >1 darkens. |
| `saturation` | `1.0` | Colour intensity. |

**Use `bayer`, not `ed`.** Measured on this Pi, decoding 198 frames of a
640x360 GIF to 480x320:

| `sws_dither` | Throughput |
|---|---|
| `bayer` | **38.6 fps** |
| `x_dither` | 31.4 fps |
| `a_dither` | 31.3 fps |
| `none` | 31.1 fps |
| `ed` | **14.3 fps** |

Ordered dither is SIMD-accelerated in swscale, so `bayer` is *faster than not
dithering at all* while also removing banding — a free win. Error-diffusion is
a serial per-pixel pass and costs ~40% of the framerate, dropping below the
panel's own 25 fps ceiling. The scaler choice, by contrast, is nearly free.

Two things worth knowing about which path applies:

- **GIFs and video never touch the PIL code.** They go ffmpeg -> framebuffer, so
  `sharp` and `dither` act through swscale, and gamma/saturation through the
  `eq` filter.
- **Stills, text and sysmon go through `pack()`**, which quantises by *rounding*
  (`(r * 31 + 127) // 255`) rather than shifting. `r >> 3` truncates, darkening
  every channel by up to 7/255 and shifting hue.

The **Diagnostic pattern** button separates the three failure modes: colour
ramps reveal banding, flat patches reveal colour error, and the 1px lines and
checkerboard reveal softness. If the fine lines look crisp but moving images
smear, that is the panel's own pixel response time, which no setting can fix.

### Colour order

This panel was verified as **true RGB565 little-endian** with a three-bar test
pattern, and `test_display.py` pins that. If you ever move the code to a
different board and red/blue come out swapped, set `BGR=1` in
`pidisplay.container` — it switches both the PIL and ffmpeg paths at once.

Hit **Test pattern** in the UI any time; left-to-right should read red, green,
blue.

## Tests

```sh
podman run --rm pidisplay:latest python3 test_display.py
```

Covers the RGB565 bit-packing against known values, letterboxing geometry,
the colour and integer validators, upload-name sanitising (including path
traversal attempts), text wrapping edge cases, `/proc` stat parsing, spec
canonicalisation and rejection, slideshow behaviour (skipping deleted files,
giving up rather than spinning when everything is missing), and state
round-tripping through disk including the corrupt-JSON path.

No hardware needed — the video and slideshow sources are lazy generators, so
nothing spawns ffmpeg or writes to the panel until the painter iterates them.
That is why `deploy.sh` can run the suite on every deploy.

## Deliberate limits

- **Video is silent.** The Zero 2 W has no analog audio out and the panel
  carries none.
- **The slideshow cycles the whole library.** Choosing a subset is not wired up;
  delete what you do not want in it, or ask and it is a small change — the spec
  already carries an explicit `names` list.
- **Flask's built-in server.** It is one LAN client driving a 480x320 panel.
- Shortcuts with a known ceiling are marked `# ponytail:` in the source, each
  naming its upgrade path.
