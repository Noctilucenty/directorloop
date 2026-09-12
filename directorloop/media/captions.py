"""Caption rendering as PNG overlays with exact fit measurements.

This ffmpeg build has no drawtext, so captions are composed with Pillow. That also
gives deterministic, measurable bounding boxes for the mechanical caption-fit check.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..domain.edit_plan import Caption, OutputProfile

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def find_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


@dataclass(frozen=True)
class CaptionLayout:
    caption_id: str
    lines: list[str]
    box: tuple[int, int, int, int]  # x0, y0, x1, y1 in output pixels
    font_px: int
    fits: bool
    chars: int
    duration_ms: int
    chars_per_second: float
    png_path: Path | None = None


class CaptionStyle:
    def __init__(self, output: OutputProfile) -> None:
        self.output = output
        self.font_px = max(28, int(output.height * 0.040))
        self.margin_x = int(output.width * 0.08)
        self.margin_y = int(output.height * 0.10)
        self.pad = int(self.font_px * 0.45)
        self.line_gap = int(self.font_px * 0.25)
        self.max_lines = 3
        self.fill = (250, 244, 230, 255)
        self.bg = (18, 16, 14, 190)


def _wrap(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if font.getlength(trial) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def layout_caption(caption: Caption, output: OutputProfile) -> CaptionLayout:
    style = CaptionStyle(output)
    font = find_font(style.font_px)
    max_width = output.width - 2 * style.margin_x - 2 * style.pad
    lines = _wrap(caption.text, font, max_width)
    line_h = style.font_px + style.line_gap
    box_w = int(max(font.getlength(line) for line in lines) + 2 * style.pad) if lines else 0
    box_h = int(len(lines) * line_h - style.line_gap + 2 * style.pad)
    x0 = (output.width - box_w) // 2
    if caption.position == "top":
        y0 = style.margin_y
    elif caption.position == "middle":
        y0 = (output.height - box_h) // 2
    else:
        y0 = output.height - style.margin_y - box_h
    box = (x0, y0, x0 + box_w, y0 + box_h)
    fits = (
        len(lines) <= style.max_lines
        and box[0] >= 0
        and box[2] <= output.width
        and box[1] >= 0
        and box[3] <= output.height
        and all(font.getlength(line) <= max_width for line in lines)
    )
    chars = len(caption.text)
    cps = chars / max(0.001, caption.duration_ms / 1000)
    return CaptionLayout(
        caption_id=caption.id,
        lines=lines,
        box=box,
        font_px=style.font_px,
        fits=fits,
        chars=chars,
        duration_ms=caption.duration_ms,
        chars_per_second=cps,
    )


def render_caption_png(caption: Caption, output: OutputProfile, out_dir: Path) -> CaptionLayout:
    """Render a full-frame transparent PNG with the caption composed at its final position."""
    layout = layout_caption(caption, output)
    style = CaptionStyle(output)
    font = find_font(style.font_px)
    key = hashlib.sha256(
        f"{caption.text}|{caption.position}|{output.width}x{output.height}|{style.font_px}".encode()
    ).hexdigest()[:16]
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path = out_dir / f"cap_{key}.png"
    if not png_path.exists():
        img = Image.new("RGBA", (output.width, output.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        x0, y0, x1, y1 = layout.box
        draw.rounded_rectangle((x0, y0, x1, y1), radius=int(style.font_px * 0.35), fill=style.bg)
        y = y0 + style.pad
        for line in layout.lines:
            lw = font.getlength(line)
            draw.text(((output.width - lw) / 2, y), line, font=font, fill=style.fill)
            y += style.font_px + style.line_gap
        tmp = png_path.with_suffix(".tmp.png")
        img.save(tmp, format="PNG", optimize=False)
        tmp.replace(png_path)
    return CaptionLayout(**{**layout.__dict__, "png_path": png_path})
