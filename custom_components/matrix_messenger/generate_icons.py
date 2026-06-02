"""
Generate icon PNGs for the matrix_messenger integration.
Run once:  python generate_icons.py
Requires:  pip install Pillow
Output:    icon.png, icon@2x.png, dark_icon.png, dark_icon@2x.png
"""
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    raise SystemExit("Pillow not found. Install it with:  pip install Pillow")


NAVY          = (26,  26,  46, 255)    # #1A1A2E  (background)
PURPLE        = (91,  45, 142, 255)    # #5B2D8E  (speech bubble)
TEAL          = ( 3, 218, 198, 255)    # #03DAC6  (badge)
WHITE         = (255, 255, 255, 255)
TRANSPARENT   = (0,   0,   0,   0)

DARK_BG       = (18,  18,  18, 255)    # near-black for dark-mode variant


def draw_icon(size: int, dark: bool) -> Image.Image:
    s = size / 256

    img = Image.new("RGBA", (size, size), TRANSPARENT)
    d   = ImageDraw.Draw(img)

    bg = DARK_BG if dark else NAVY

    # ── Outer rounded background ────────────────────────────────────────────
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(48 * s), fill=bg)

    # ── Speech bubble ───────────────────────────────────────────────────────
    # Body (rounded rect, top + sides)
    bx0, by0 = int(36 * s), int(36 * s)
    bx1, by1 = int(220 * s), int(168 * s)
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=int(16 * s), fill=PURPLE)

    # Tail triangle pointing down-center
    cx = int(128 * s)
    ty = int(168 * s)
    tw = int(28 * s)
    th = int(34 * s)
    d.polygon(
        [(cx - tw, ty), (cx + tw, ty), (cx, ty + th)],
        fill=PURPLE,
    )

    # ── Left bracket [ ──────────────────────────────────────────────────────
    lx  = int(60 * s)
    bw  = int(11 * s)
    bh  = int(60 * s)
    cap = int(26 * s)
    cr  = int(3 * s)
    by_top = int(78 * s)
    by_bot = int(127 * s)

    d.rounded_rectangle([lx, by_top, lx + bw, by_top + bh], radius=cr, fill=WHITE)        # vertical
    d.rounded_rectangle([lx, by_top, lx + cap, by_top + bw], radius=cr, fill=WHITE)        # top cap
    d.rounded_rectangle([lx, by_bot, lx + cap, by_bot + bw], radius=cr, fill=WHITE)        # bottom cap

    # ── Right bracket ] ─────────────────────────────────────────────────────
    rx = int(185 * s)
    d.rounded_rectangle([rx, by_top, rx + bw, by_top + bh], radius=cr, fill=WHITE)
    d.rounded_rectangle([rx - cap + bw, by_top, rx + bw, by_top + bw], radius=cr, fill=WHITE)
    d.rounded_rectangle([rx - cap + bw, by_bot, rx + bw, by_bot + bw], radius=cr, fill=WHITE)

    # ── M letter (polyline: left-up, peak, right-up, right-down) ────────────
    sw = int(12 * s)
    pts = [
        (int(97  * s), int(132 * s)),
        (int(97  * s), int(84  * s)),
        (int(128 * s), int(114 * s)),
        (int(159 * s), int(84  * s)),
        (int(159 * s), int(132 * s)),
    ]
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        d.line([x0, y0, x1, y1], fill=WHITE, width=sw)
    # round caps
    for px, py in pts:
        r = sw // 2
        d.ellipse([px - r, py - r, px + r, py + r], fill=WHITE)

    # ── Teal badge with three dots ───────────────────────────────────────────
    bc = int(196 * s)
    br = int(36 * s)
    d.ellipse([bc - br, bc - br, bc + br, bc + br], fill=TEAL)

    dot_r = int(7 * s)
    for dx in (-12, 0, 12):
        cx2 = bc + int(dx * s)
        d.ellipse([cx2 - dot_r, bc - dot_r, cx2 + dot_r, bc + dot_r], fill=bg)

    return img


HERE = Path(__file__).parent / "brand"
HERE.mkdir(exist_ok=True)

variants = [
    ("icon.png",         256, False),
    ("icon@2x.png",      512, False),
    ("dark_icon.png",    256, True),
    ("dark_icon@2x.png", 512, True),
]

for filename, size, dark in variants:
    path = HERE / filename
    draw_icon(size, dark).save(path, "PNG")
    print(f"  created  brand/{path.name}  ({size}×{size})")

print("\nDone. Restart Home Assistant to pick up the new icons.")
