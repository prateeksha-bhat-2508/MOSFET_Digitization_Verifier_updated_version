"""Readable evidence drawings in the original source-image coordinate system.

These functions only draw recorded coordinates. They do not select a scale,
move calibration points, connect extraction samples, or infer curve identity.
The source is pasted at (0, 0); legends extend the canvas below the source.
"""
from __future__ import annotations

import io
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


X_COLOR = (174, 0, 35)
Y_COLOR = (0, 66, 153)
INK = (25, 31, 40)
WHITE = (255, 255, 255)
PALETTE = (
    (174, 0, 35), (0, 77, 157), (0, 112, 65), (111, 42, 142),
    (164, 77, 0), (0, 112, 124), (163, 0, 104), (45, 45, 45),
)


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _font(size, bold=False):
    names = [r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, max(12, int(size)))
        except OSError:
            pass
    return ImageFont.load_default()


def _text_width(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap(draw, text, font, width):
    """Wrap every character; long dataset labels are never clipped or dropped."""
    lines, line = [], ""
    for token in str(text).split(" "):
        candidate = (line + " " + token).strip()
        if line and _text_width(draw, candidate, font) > width:
            lines.append(line)
            line = ""
        while token and _text_width(draw, token, font) > width:
            end = 1
            while end < len(token) and _text_width(draw, token[:end + 1], font) <= width:
                end += 1
            lines.append(token[:end])
            token = token[end:]
        line = (line + " " + token).strip()
    if line:
        lines.append(line)
    return lines or [""]


def _legend(source, items, font_size, title=None):
    """Return a canvas and legend placements without reducing source pixels."""
    font = _font(font_size, True)
    probe = ImageDraw.Draw(source)
    pad = max(8, round(font_size * .35))
    symbol_space = round(font_size * 1.6)
    columns = 2 if source.width >= font_size * 16 else 1
    width = source.width / columns
    line_height = round(font_size * 1.32)
    top = source.height + pad
    title_lines = _wrap(probe, title, font, source.width - 2 * pad) if title else []
    if title_lines:
        top += len(title_lines) * line_height + pad
    placements = []
    for index in range(0, len(items), columns):
        row = items[index:index + columns]
        wrapped = [_wrap(probe, item[1], font, max(20, width - symbol_space - 2 * pad)) for item in row]
        for col, (item, lines) in enumerate(zip(row, wrapped)):
            placements.append((pad + col * width, top, item, lines))
        top += max(len(lines) for lines in wrapped) * line_height + pad
    canvas = Image.new("RGB", (source.width, max(source.height, math.ceil(top + pad))), WHITE)
    canvas.paste(source, (0, 0))
    draw = ImageDraw.Draw(canvas)
    if items or title:
        draw.line((0, source.height + 1, source.width, source.height + 1), fill=(170, 178, 188), width=max(1, font_size // 16))
    yy = source.height + pad
    for line in title_lines:
        draw.text((pad, yy), line, font=font, fill=INK)
        yy += line_height
    for x, y, item, lines in placements:
        color = item[0]
        for n, line in enumerate(lines):
            draw.text((x + symbol_space, y + n * line_height), line, font=font, fill=color)
    return canvas, placements, font, line_height


def _ring(draw, x, y, role, radius, stroke, shared=False):
    color = X_COLOR if role.startswith("X") else Y_COLOR
    r = radius * (1.18 if shared and role.startswith("X") else .82 if shared else 1)
    box = (x - r, y - r, x + r, y + r)
    def outline(fill, width):
        if role == "X2":
            draw.rectangle(box, outline=fill, width=width)
        elif role == "Y1":
            draw.line(((x, y - r), (x + r, y), (x, y + r), (x - r, y), (x, y - r)), fill=fill, width=width, joint="curve")
        else:
            draw.ellipse(box, outline=fill, width=width)
    outline(WHITE, stroke + 4)
    outline(color, stroke)
    # Coincident X/Y references retain one common centre, with red horizontal
    # and blue vertical crosshairs instead of obscuring one another.
    directions = [(x - r - 4, y, x + r + 4, y)] if shared and role.startswith("X") else (
        [(x, y - r - 4, x, y + r + 4)] if shared else
        [(x - r - 4, y, x + r + 4, y), (x, y - r - 4, x, y + r + 4)])
    for line in directions:
        draw.line(line, fill=WHITE, width=stroke + 3)
        draw.line(line, fill=color, width=stroke)
    return color


def _label_box(source_gray, draw, x, y, parts, font, radius, occupied, centres):
    font_size = getattr(font, "size", 16)
    pad = max(5, round(font_size * .23))
    # Multi-axis projects can have several references at the same pixel. Wrap
    # the complete role labels rather than clipping them at the image edge.
    max_width = max(16, source_gray.width - 2 * pad - 6)
    lines, current = [], []
    for label, color in parts:
        for chunk in _wrap(draw, label, font, max_width):
            joined = " / ".join(text for text, _ in current + [(chunk, color)])
            if current and _text_width(draw, joined, font) > max_width:
                lines.append(current)
                current = []
            current.append((chunk, color))
    if current:
        lines.append(current)
    line_height = round(font_size * 1.3)
    if len(lines) * line_height + 2 * pad > source_gray.height - 6:
        # The external legend still lists every axis/role/value. This is only
        # a spatial fallback for images too small for all coincident labels.
        label = f"{len(parts)} markers; see key"
        lines = [[(chunk, INK)] for chunk in _wrap(draw, label, font, max_width)]
    width = max(_text_width(draw, " / ".join(text for text, _ in line), font) for line in lines) + 2 * pad
    height = len(lines) * line_height + 2 * pad
    candidates = []
    for factor in (1.65, 3.2, 5.0, 7.0):
        gap = radius * factor
        for dx, dy in ((gap, -height - gap), (gap, gap), (-width - gap, -height - gap), (-width - gap, gap),
                       (gap, -height / 2), (-width - gap, -height / 2), (-width / 2, -height - gap), (-width / 2, gap)):
            left = min(max(3, x + dx), max(3, source_gray.width - width - 3))
            top = min(max(3, y + dy), max(3, source_gray.height - height - 3))
            box = (left, top, left + width, top + height)
            crop = source_gray.crop(tuple(round(v) for v in box))
            dark = sum(crop.histogram()[:150])
            overlap = sum(max(0, min(box[2], old[2]) - max(box[0], old[0])) * max(0, min(box[3], old[3]) - max(box[1], old[1])) for old in occupied)
            hidden_centres = sum(box[0] - radius < cx < box[2] + radius and box[1] - radius < cy < box[3] + radius for cx, cy in centres)
            distance = math.hypot(left + width / 2 - x, top + height / 2 - y)
            candidates.append((dark * 100 + overlap * 1000 + hidden_centres * 1000000 + distance / max(source_gray.size), box))
    box = min(candidates, key=lambda item: item[0])[1]
    occupied.append(box)
    end = (min(max(x, box[0]), box[2]), min(max(y, box[1]), box[3]))
    border = parts[0][1] if len(parts) == 1 else INK
    line_width = max(2, round(font_size / 13))
    leader = [(x, y), end]
    # A straight vertical leader under an X marker can cross its printed tick
    # value. Step diagonally away from the centre before approaching the box.
    if box[0] <= x <= box[2] and not box[1] <= y <= box[3]:
        side = 1 if x < source_gray.width / 2 else -1
        direction = 1 if box[1] > y else -1
        bend = (x + side * radius * 1.7, y + direction * radius * 1.7)
        end = (min(max(bend[0], box[0]), box[2]), end[1])
        leader = [(x, y), bend, end]
    draw.line(leader, fill=WHITE, width=line_width + 3)
    draw.line(leader, fill=border, width=line_width)
    draw.rounded_rectangle(box, radius=pad, fill=WHITE, outline=border, width=line_width)
    for row, line in enumerate(lines):
        xx, yy = box[0] + pad, box[1] + pad + row * line_height
        for index, (label, color) in enumerate(line):
            if index:
                separator = " / "
                draw.text((xx, yy), separator, fill=INK, font=font)
                xx += _text_width(draw, separator, font)
            draw.text((xx, yy), label, fill=color, font=font)
            xx += _text_width(draw, label, font)


def draw_calibration(image_bytes, axes, out_path):
    source = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    size = max(18, round(max(source.size) / 28))
    radius, stroke = max(8, max(source.size) / 63), max(2, round(max(source.size) / 280))
    markers, legend = [], []
    for index, axis in enumerate(axes):
        axis_name = str(_get(axis, "name", "") or f"Axis {index + 1}")
        for cp in _get(axis, "calibration", []) or []:
            x, y = _finite(_get(cp, "px")), _finite(_get(cp, "py"))
            if x is None or y is None:
                continue
            role = str(_get(cp, "role", "") or "Marker")
            value = _get(cp, "dx") if role.startswith("X") else _get(cp, "dy")
            color = X_COLOR if role.startswith("X") else Y_COLOR
            label = (f"A{index + 1} " if len(axes) > 1 else "") + role
            name = (axis_name + ": " if len(axes) > 1 else "") + f"{role} = {value if value is not None else 'not stored'}"
            markers.append((x, y, role, label, color))
            legend.append((color, name, role))
    canvas, placements, font, line_height = _legend(source, legend, size)
    draw = ImageDraw.Draw(canvas)
    # Only identical coordinates share a label; nearby points are not averaged.
    centres = {}
    for marker in markers:
        centres.setdefault(marker[:2], []).append(marker)
    occupied = []
    gray = source.convert("L")
    for (x, y), group in centres.items():
        if not (0 <= x < source.width and 0 <= y < source.height):
            continue
        parts = [(entry[3], entry[4]) for entry in group]
        _label_box(gray, draw, x, y, parts, font, radius, occupied, list(centres))
    for (x, y), group in centres.items():
        if 0 <= x < source.width and 0 <= y < source.height:
            for _, _, role, _, _ in group:
                _ring(draw, x, y, role, radius, stroke, len(group) > 1)
    for x, y, item, _ in placements:
        _ring(draw, x + size * .65, y + line_height / 2, item[2], size * .36, max(2, stroke - 1))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)


def draw_extraction(image_bytes, datasets, out_path):
    source = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    size = max(16, round(max(source.size) / 31))
    legend = []
    for index, ds in enumerate(datasets):
        points = _get(ds, "points", []) or []
        name = str(_get(ds, "name", "") or f"Dataset {index + 1}")
        legend.append((PALETTE[index % len(PALETTE)], f"{name} ({len(points)} points)", index))
    canvas, placements, _, line_height = _legend(source, legend, size, "WPD extraction points")
    draw = ImageDraw.Draw(canvas)
    radius = max(3.0, max(source.size) / 145)
    edge, halo = max(1, round(radius / 4)), max(1, round(radius / 3))
    def point(x, y, color, r=radius):
        draw.ellipse((x - r - halo, y - r - halo, x + r + halo, y + r + halo), fill=WHITE)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color, outline=INK, width=edge)
    for index, ds in enumerate(datasets):
        color = PALETTE[index % len(PALETTE)]
        for pair in _get(ds, "points", []) or []:
            if len(pair) < 2:
                continue
            x, y = _finite(pair[0]), _finite(pair[1])
            if x is not None and y is not None and 0 <= x < source.width and 0 <= y < source.height:
                point(x, y, color)
    for x, y, item, _ in placements:
        point(x + size * .65, y + line_height / 2, item[0], max(radius, size * .28))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
