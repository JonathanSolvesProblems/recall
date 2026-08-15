"""Compose the eight Devpost gallery images from captured screenshots.

A Devpost gallery is a carousel a judge swipes without reading the writeup, so
each image has to say what it is on its own. Every one gets the same 3:2 frame,
a caption band in the project's own paper-and-ink palette, and the screenshot
fitted whole rather than cropped, because a cropped screenshot of a number is
how a number stops being checkable.

    python scripts/build_preview_gallery.py
"""

from __future__ import annotations

import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 1800, 1200
PAPER, INK, MUTED, RULE, STOP = "#faf8f4", "#171512", "#6f6a61", "#e2ddd3", "#9d1c09"
PAD, BAND = 54, 196

SRC = pathlib.Path("broll/preview/_src")
OUT = pathlib.Path("broll/preview")

# (source, headline, one line of context)
SHOTS = [
    ("01.png", "1. An answer, and the exact evidence behind it",
     "Answered from live openFDA data. The claim it read is recorded with its version, "
     "and the bold rule marks the read the verdict actually leaned on."),
    ("02.png", "2. Nothing is overwritten",
     "The FDA escalates the lot. Version 1 is retracted and version 2 asserted under the "
     "same key, in one transaction, so every past answer still points at what it read."),
    ("03.png", "3. The query a vector store cannot express",
     "A join from decision, to the claim version it consumed, to the claim. Twelve standing "
     "answers rest on evidence that has since changed, each listing the version it stood on."),
    # Sweep wall-clock varies run to run (3.7 to 4.5s observed). The caption
    # must quote what this particular capture shows, not a remembered figure.
    ("04.png", "4. Twelve re-decided, nine reversed, in 3.7 seconds",
     "Each answer is re-decided against what is believed now. Three had already said stop, "
     "so they are reaffirmed rather than corrected, and deliberately not messaged."),
    ("05.png", "5. Corrections addressed to a person",
     "Each written in the same transaction as the correction that justifies it, and keyed so "
     "a replayed sweep cannot send a second time."),
    ("06.png", "6. Four CockroachDB tools, all load-bearing",
     "Remove any one and something in the story stops working."),
    ("07.png", "7. An entire region removed, still answering",
     "Nine nodes across three regions under SURVIVE REGION FAILURE, which places five "
     "replicas so no single region holds a majority."),
    ("08.png", "8. Replay that outlives the garbage collector",
     "Asked what it believed 45 days ago, bitemporal reconstruction answers exactly where "
     "AS OF SYSTEM TIME fails on the GC threshold. Inside the window the two agree."),
]


def font(size: int, bold: bool = False):
    for name in (("seguisb.ttf", "segoeuib.ttf") if bold else ("segoeui.ttf",)):
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def wrap(draw, text, f, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=f) <= width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def trim_margins(im: Image.Image) -> Image.Image:
    """Drop the empty paper columns either side of the app's centred column.

    A full-page screenshot at a 1920 viewport is about 40 percent blank margin,
    because the app caps its content at 1120px. Fitting that whole frame into
    the gallery box wastes the space on nothing and shrinks the text a judge is
    meant to read. Only horizontal margins are trimmed; vertical cropping would
    cut content.
    """
    g = im.convert("L")
    w, h = g.size
    px = g.load()
    step = max(1, h // 200)
    def blank(x: int) -> bool:
        return all(px[x, y] > 244 for y in range(0, h, step))
    left = 0
    while left < w - 1 and blank(left):
        left += 1
    right = w - 1
    while right > left and blank(right):
        right -= 1
    pad = 24
    left, right = max(0, left - pad), min(w - 1, right + pad)
    return im.crop((left, 0, right + 1, h)) if right - left > w * 0.3 else im


def compose(src: pathlib.Path, title: str, sub: str, dest: pathlib.Path) -> None:
    canvas = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(canvas)

    f_title, f_sub = font(46, bold=True), font(28)
    d.text((PAD, 44), title, font=f_title, fill=INK)
    y = 44 + 58
    for line in wrap(d, sub, f_sub, W - PAD * 2):
        d.text((PAD, y), line, font=f_sub, fill=MUTED)
        y += 38

    d.line([(PAD, BAND - 16), (W - PAD, BAND - 16)], fill=RULE, width=2)

    shot = Image.open(src).convert("RGB")
    # The three prepared frames are already full-bleed designs; only the
    # live page screenshots carry dead margin.
    if shot.width >= 1900:
        shot = trim_margins(shot)
    box_w, box_h = W - PAD * 2, H - BAND - PAD
    scale = min(box_w / shot.width, box_h / shot.height)
    shot = shot.resize((round(shot.width * scale), round(shot.height * scale)),
                       Image.LANCZOS)
    x = (W - shot.width) // 2
    yy = BAND + (box_h - shot.height) // 2
    d.rectangle([x - 1, yy - 1, x + shot.width, yy + shot.height], outline=RULE)
    canvas.paste(shot, (x, yy))
    canvas.save(dest, optimize=True)
    print(f"  {dest.name}  {dest.stat().st_size // 1024} KB")


def main() -> int:
    if not SRC.exists():
        sys.exit(f"{SRC} not found. Capture the source screenshots first.")
    for i, (name, title, sub) in enumerate(SHOTS, 1):
        compose(SRC / name, title, sub, OUT / f"{i:02d}-{title.split('. ')[1][:34]
                .lower().replace(' ', '-').replace(',', '')}.png")
    print(f"\n{len(SHOTS)} gallery images in {OUT} (Devpost allows 8).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
