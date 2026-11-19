"""Prosopo puzzle matching: drop the overlay onto the darkened cut.

The provider paints decoy jigsaw silhouettes, then cuts the real piece out of
the 300x200 board. The cut is darkened (and may include inner lines from
overlapping decoys). The widget never sends the target; it only shows the
piece on transparency at ``originX/originY`` (the piece centre) and records
every pointer sample until mouseup.

Matching is masked normalised cross-correlation of luminance and of edges,
plus darkness under the silhouette. Prosopo adds a few units of noise and a
brightness shift so a pixel-exact copy cannot be slid onto the hole; mean
and variance normalisation remove that, and the inner-line edge map is the
unique fingerprint of the cut.
"""

from __future__ import annotations

import base64
import hashlib
import io
import math
import random
import re
import sys
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

BOARD_WIDTH = 300
BOARD_HEIGHT = 200

# A zero-mean luminance template with no variance used to be replaced by the
# binary mask, which saturates NCC at 1 against a dull window. Live failures
# were ncc=1.000 with edge under ~0.25; those peaks are not the cut.
FLAT_TEMPLATE_SQ = 1e-3
SATURATED_LUMA_NCC = 0.999
WEAK_EDGE_NCC = 0.25


@dataclass(frozen=True)
class PuzzleMatch:
    x: float
    y: float
    score: float
    ncc: float
    ncc_edge: float
    darkness: float


@dataclass(frozen=True)
class BoardRect:
    left: float
    top: float
    width: float
    height: float

    def to_viewport(self, x: float, y: float) -> tuple[float, float]:
        if self.width <= 0 or self.height <= 0:
            return (x, y)
        return (
            self.left + (x / BOARD_WIDTH) * self.width,
            self.top + (y / BOARD_HEIGHT) * self.height,
        )


def decode_image(data: str | bytes) -> np.ndarray:
    """Decode a PNG/WebP data URI or raw bytes to an HxWx4 uint8 array."""
    raw: bytes
    if isinstance(data, str):
        payload = data.strip()
        if payload.startswith("data:") and "," in payload:
            payload = payload.split(",", 1)[1]
        raw = base64.b64decode(payload)
    else:
        raw = data
    image = Image.open(io.BytesIO(raw))
    return np.array(image.convert("RGBA"))


def scale_to_board(pixels: np.ndarray) -> np.ndarray:
    """Scale a screenshot crop to the 300x200 board the matcher uses."""
    image = Image.fromarray(pixels)
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    if image.size != (BOARD_WIDTH, BOARD_HEIGHT):
        image = image.resize((BOARD_WIDTH, BOARD_HEIGHT), Image.Resampling.BILINEAR)
    return np.array(image)


def crop_css_rect(
    screenshot: np.ndarray,
    left: float,
    top: float,
    width: float,
    height: float,
    dpr: float,
) -> np.ndarray | None:
    """Crop a CSS-pixel rect out of a device-pixel screenshot."""
    try:
        scale = float(dpr) if dpr else 1.0
        x = float(left) * scale
        y = float(top) * scale
        w = float(width) * scale
        h = float(height) * scale
    except (TypeError, ValueError):
        return None
    if screenshot is None or w < 8 or h < 8:
        return None
    x0 = max(0, int(round(x)))
    y0 = max(0, int(round(y)))
    x1 = min(int(screenshot.shape[1]), int(round(x + w)))
    y1 = min(int(screenshot.shape[0]), int(round(y + h)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    return screenshot[y0:y1, x0:x1]


def raster_digest(background: np.ndarray, piece: np.ndarray) -> str:
    """Short hash of the two rasters; no pixels in the log line.

    The piece is hashed in full (CSS canvas output is stable). The board is
    a screenshot, so a 1px crop jitter must not look like a new puzzle.
    """
    hasher = hashlib.sha256()
    piece_c = np.ascontiguousarray(piece)
    hasher.update(np.asarray(piece_c.shape, dtype=np.int64).tobytes())
    hasher.update(piece_c.tobytes())
    bg = np.ascontiguousarray(background)
    hasher.update(np.asarray(bg.shape, dtype=np.int64).tobytes())
    image = Image.fromarray(bg)
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    coarse = np.array(image.resize((30, 20), Image.Resampling.BILINEAR))
    hasher.update((coarse // 8).astype(np.uint8).tobytes())
    return hasher.hexdigest()[:12]


def _parse_css_length(token: str, reference: float) -> float:
    text = str(token or "").strip().lower()
    if not text:
        return 0.0
    if text.endswith("%"):
        return float(text[:-1]) / 100.0 * reference
    if text.endswith("px"):
        text = text[:-2]
    return float(text)


def _parse_bg_size(
    value: str,
    img_w: float,
    img_h: float,
    box_w: float,
    box_h: float,
) -> tuple[float, float]:
    text = str(value or "auto").strip().lower()
    if text == "cover":
        scale = max(box_w / img_w, box_h / img_h) if img_w and img_h else 1.0
        return img_w * scale, img_h * scale
    if text == "contain":
        scale = min(box_w / img_w, box_h / img_h) if img_w and img_h else 1.0
        return img_w * scale, img_h * scale
    parts = text.split()
    if not parts or (len(parts) == 1 and parts[0] == "auto"):
        return img_w, img_h
    if len(parts) == 1:
        width = _parse_css_length(parts[0], box_w)
        return width, width * img_h / img_w if img_w else width
    width_tok, height_tok = parts[0], parts[1]
    if width_tok == "auto" and height_tok == "auto":
        return img_w, img_h
    if width_tok == "auto":
        height = _parse_css_length(height_tok, box_h)
        return (height * img_w / img_h if img_h else height), height
    if height_tok == "auto":
        width = _parse_css_length(width_tok, box_w)
        return width, (width * img_h / img_w if img_w else width)
    return (
        _parse_css_length(width_tok, box_w),
        _parse_css_length(height_tok, box_h),
    )


def _parse_bg_position(
    value: str,
    size_w: float,
    size_h: float,
    box_w: float,
    box_h: float,
) -> tuple[float, float]:
    keywords = {
        "left": "0%",
        "top": "0%",
        "right": "100%",
        "bottom": "100%",
        "center": "50%",
    }
    tokens = [
        keywords.get(tok, tok)
        for tok in str(value or "0px 0px").strip().lower().split()
        if tok not in {"from", "at"}
    ]
    if not tokens:
        tokens = ["0px", "0px"]
    if len(tokens) == 1:
        tokens = [tokens[0], "50%"]
    # Used values from getComputedStyle are typically "Xpx Ypx". Percentages
    # align the image relative to (box - size), matching CSS background-position.
    def axis(token: str, size: float, box: float) -> float:
        if token.endswith("%"):
            return (box - size) * float(token[:-1]) / 100.0
        return _parse_css_length(token, box)

    return axis(tokens[0], size_w, box_w), axis(tokens[1], size_h, box_h)


def _resize_rgba(pixels: np.ndarray, width: int, height: int) -> np.ndarray:
    image = Image.fromarray(pixels)
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    width = max(1, int(round(width)))
    height = max(1, int(round(height)))
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.BILINEAR)
    return np.array(image)


def _blit_rgba(dst: np.ndarray, src: np.ndarray, x: float, y: float) -> None:
    x0 = int(round(x))
    y0 = int(round(y))
    src_h, src_w = src.shape[:2]
    dst_h, dst_w = dst.shape[:2]
    src_x0 = max(0, -x0)
    src_y0 = max(0, -y0)
    dst_x0 = max(0, x0)
    dst_y0 = max(0, y0)
    width = min(src_w - src_x0, dst_w - dst_x0)
    height = min(src_h - src_y0, dst_h - dst_y0)
    if width <= 0 or height <= 0:
        return
    patch = src[src_y0 : src_y0 + height, src_x0 : src_x0 + width].astype(np.float32)
    out = dst[dst_y0 : dst_y0 + height, dst_x0 : dst_x0 + width].astype(np.float32)
    alpha = patch[:, :, 3:4] / 255.0
    out[:, :, :3] = patch[:, :, :3] * alpha + out[:, :, :3] * (1.0 - alpha)
    out[:, :, 3:4] = np.clip(patch[:, :, 3:4] + out[:, :, 3:4] * (1.0 - alpha), 0, 255)
    dst[dst_y0 : dst_y0 + height, dst_x0 : dst_x0 + width] = np.clip(out, 0, 255).astype(
        np.uint8
    )


def paint_css_image(
    image: np.ndarray,
    box_w: int,
    box_h: int,
    *,
    size: str = "auto",
    position: str = "0px 0px",
    repeat: str = "no-repeat",
) -> np.ndarray:
    """Paint ``background-image`` into a box using CSS size/position/repeat."""
    box_w = max(1, int(box_w))
    box_h = max(1, int(box_h))
    src = image
    if src.ndim == 2:
        src = np.stack([src, src, src, np.full_like(src, 255)], axis=-1)
    elif src.shape[2] == 3:
        alpha = np.full(src.shape[:2] + (1,), 255, dtype=np.uint8)
        src = np.concatenate([src, alpha], axis=2)
    img_h, img_w = src.shape[:2]
    size_w, size_h = _parse_bg_size(size, float(img_w), float(img_h), float(box_w), float(box_h))
    pos_x, pos_y = _parse_bg_position(position, size_w, size_h, float(box_w), float(box_h))
    scaled = _resize_rgba(src, max(1, int(round(size_w))), max(1, int(round(size_h))))
    canvas = np.zeros((box_h, box_w, 4), dtype=np.uint8)
    repeat_token = str(repeat or "no-repeat").split()[0]
    tile_w = max(1, scaled.shape[1])
    tile_h = max(1, scaled.shape[0])
    do_x = repeat_token in {"repeat", "repeat-x"}
    do_y = repeat_token in {"repeat", "repeat-y"}
    if not do_x and not do_y:
        _blit_rgba(canvas, scaled, pos_x, pos_y)
        return canvas
    start_x = pos_x % tile_w
    if start_x > 0:
        start_x -= tile_w
    start_y = pos_y % tile_h
    if start_y > 0:
        start_y -= tile_h
    if not do_x:
        start_x = pos_x
    if not do_y:
        start_y = pos_y
    y = start_y
    while y < (box_h if do_y else start_y + 1):
        x = start_x
        while x < (box_w if do_x else start_x + 1):
            _blit_rgba(canvas, scaled, x, y)
            if not do_x:
                break
            x += tile_w
        if not do_y:
            break
        y += tile_h
    return canvas


def _clip_path_mask(clip_path: str, box_w: int, box_h: int) -> np.ndarray | None:
    text = str(clip_path or "").strip()
    if not text or text == "none":
        return None
    mask = Image.new("L", (box_w, box_h), 0)
    draw = ImageDraw.Draw(mask)
    poly = re.match(r"polygon\(\s*(.+)\s*\)", text, re.I | re.S)
    if poly:
        points: list[tuple[float, float]] = []
        for part in poly.group(1).split(","):
            bits = part.strip().split()
            if len(bits) < 2:
                continue
            points.append(
                (
                    _parse_css_length(bits[0], float(box_w)),
                    _parse_css_length(bits[1], float(box_h)),
                )
            )
        if len(points) >= 3:
            draw.polygon(points, fill=255)
            return np.array(mask)
    circle = re.match(
        r"circle\(\s*([^ ]+)\s*(?:at\s+([^ ]+)\s+([^ ]+))?\s*\)",
        text,
        re.I,
    )
    if circle:
        radius = _parse_css_length(circle.group(1), float(min(box_w, box_h)))
        cx = _parse_css_length(circle.group(2) or "50%", float(box_w))
        cy = _parse_css_length(circle.group(3) or "50%", float(box_h))
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=255)
        return np.array(mask)
    return None


def rasterize_css_layer(
    image: np.ndarray,
    box_w: int,
    box_h: int,
    *,
    size: str = "auto",
    position: str = "0px 0px",
    repeat: str = "no-repeat",
    mask_image: np.ndarray | None = None,
    mask_size: str = "auto",
    mask_position: str = "0px 0px",
    mask_repeat: str = "no-repeat",
    clip_path: str = "none",
) -> np.ndarray:
    """Rasterize a CSS background into ``box_w`` x ``box_h`` with mask alpha."""
    layer = paint_css_image(
        image, box_w, box_h, size=size, position=position, repeat=repeat
    )
    if mask_image is not None:
        painted_mask = paint_css_image(
            mask_image,
            box_w,
            box_h,
            size=mask_size,
            position=mask_position,
            repeat=mask_repeat,
        )
        if painted_mask.shape[2] >= 4 and float(painted_mask[:, :, 3].max()) > 0:
            mask_alpha = painted_mask[:, :, 3].astype(np.float32) / 255.0
        else:
            mask_alpha = _luma(painted_mask) / 255.0
        layer = layer.copy()
        layer[:, :, 3] = np.clip(layer[:, :, 3].astype(np.float32) * mask_alpha, 0, 255).astype(
            np.uint8
        )
    clip_mask = _clip_path_mask(clip_path, layer.shape[1], layer.shape[0])
    if clip_mask is not None:
        layer = layer.copy()
        layer[:, :, 3] = np.clip(
            layer[:, :, 3].astype(np.float32) * (clip_mask.astype(np.float32) / 255.0),
            0,
            255,
        ).astype(np.uint8)
    return layer


def rasterize_piece_spec(spec: dict[str, Any]) -> np.ndarray:
    """Build a piece RGBA array from extract CSS fields or a ready PNG."""
    piece_uri = spec.get("piece")
    if piece_uri:
        return decode_image(str(piece_uri))
    image_uri = spec.get("image") or spec.get("backgroundImage")
    if not image_uri:
        raise ValueError("piece spec has no image")
    source = decode_image(str(image_uri))
    box_w = int(round(float(spec.get("boxWidth") or spec.get("width") or source.shape[1])))
    box_h = int(round(float(spec.get("boxHeight") or spec.get("height") or source.shape[0])))
    mask_pixels = None
    mask_uri = spec.get("maskImage") or spec.get("mask")
    if mask_uri:
        mask_pixels = decode_image(str(mask_uri))
    return rasterize_css_layer(
        source,
        box_w,
        box_h,
        size=str(spec.get("backgroundSize") or "auto"),
        position=str(spec.get("backgroundPosition") or "0px 0px"),
        repeat=str(spec.get("backgroundRepeat") or "no-repeat"),
        mask_image=mask_pixels,
        mask_size=str(spec.get("maskSize") or "auto"),
        mask_position=str(spec.get("maskPosition") or "0px 0px"),
        mask_repeat=str(spec.get("maskRepeat") or "no-repeat"),
        clip_path=str(spec.get("clipPath") or "none"),
    )


def _luma(pixels: np.ndarray) -> np.ndarray:
    rgb = pixels.astype(np.float32)
    if rgb.ndim == 2:
        return rgb
    return 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]


def _sobel(luma: np.ndarray) -> np.ndarray:
    gx = np.zeros_like(luma, dtype=np.float32)
    gy = np.zeros_like(luma, dtype=np.float32)
    gx[:, 1:-1] = luma[:, 2:] - luma[:, :-2]
    gy[1:-1, :] = luma[2:, :] - luma[:-2, :]
    return np.hypot(gx, gy)


def _fft_correlate(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """``out[y, x] = sum(image[y:y+kh, x:x+kw] * kernel)``."""
    height, width = image.shape
    kh, kw = kernel.shape
    fft_h = height + kh - 1
    fft_w = width + kw - 1
    freq_i = np.fft.rfft2(image, s=(fft_h, fft_w))
    freq_k = np.fft.rfft2(kernel[::-1, ::-1], s=(fft_h, fft_w))
    conv = np.fft.irfft2(freq_i * freq_k, s=(fft_h, fft_w)).real
    return conv[kh - 1 : kh - 1 + height - kh + 1, kw - 1 : kw - 1 + width - kw + 1]


def _erode(mask: np.ndarray, radius: int) -> np.ndarray:
    """Binary erosion; drops the piece rim that the hole does not share."""
    if radius <= 0:
        return mask.astype(np.float32)
    out = mask.astype(np.float32)
    for _ in range(int(radius)):
        padded = np.pad(out, 1, mode="constant", constant_values=0.0)
        neigh = np.stack(
            [
                padded[0:-2, 0:-2],
                padded[0:-2, 1:-1],
                padded[0:-2, 2:],
                padded[1:-1, 0:-2],
                padded[1:-1, 1:-1],
                padded[1:-1, 2:],
                padded[2:, 0:-2],
                padded[2:, 1:-1],
                padded[2:, 2:],
            ]
        )
        out = neigh.min(axis=0)
    return out


def _prepare_piece(
    piece: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float, int]:
    """Return piece luma, silhouette mask, interior mask, template, sq, opaque."""
    piece_arr = piece.astype(np.float32)
    if piece_arr.ndim != 3 or piece_arr.shape[2] < 3:
        raise ValueError("piece must be an RGBA (or RGB) image")
    if piece_arr.shape[2] == 4:
        alpha = piece_arr[:, :, 3] / 255.0
    else:
        alpha = np.ones(piece_arr.shape[:2], dtype=np.float32)
    piece_luma = _luma(piece_arr)
    mask = (alpha >= 0.12).astype(np.float32)
    n_mask = float(mask.sum())
    if n_mask < 20:
        raise RuntimeError("puzzle piece has no opaque pixels")
    sh, sw = mask.shape
    interior = _erode(mask, max(2, int(round(0.07 * max(sh, sw)))))
    n_interior = float(interior.sum())
    luma_mask = interior if n_interior >= 20 else mask
    n_luma = float(luma_mask.sum()) or n_mask
    tmean = float((piece_luma * luma_mask).sum() / n_luma)
    template = (piece_luma - tmean) * luma_mask
    template_sq = float((template * template).sum())
    return piece_luma, mask, luma_mask, template, template_sq, int(n_mask)


def piece_template_stats(piece: np.ndarray) -> dict[str, float]:
    """Interior variance and opaque count used to skip flat overlays."""
    _piece_luma, _mask, _luma_mask, _template, template_sq, opaque = _prepare_piece(piece)
    return {
        "template_sq": template_sq,
        "opaque": float(opaque),
        "width": float(piece.shape[1]),
        "height": float(piece.shape[0]),
    }


def match_piece(
    background: np.ndarray,
    piece: np.ndarray,
    origin: tuple[float, float],
    *,
    exclude_radius: float | None = None,
    info: dict[str, Any] | None = None,
    log: bool = True,
) -> PuzzleMatch | None:
    """Locate the darkened cut that matches ``piece``.

    ``origin`` is the current centre of the overlay in board pixels, and is
    excluded so a leftover composite cannot win. Returned ``x/y`` are the
    centre the widget should be dragged to (``finalX`` / ``finalY``).

    Returns ``None`` when the piece interior has no luminance variance, or
    when the winning peak is a saturated luminance NCC with a weak edge
    score (a flat-template / dull-window artefact, not the cut).
    """
    bg_luma = _luma(background)
    piece_luma, mask, luma_mask, template, template_sq, opaque = _prepare_piece(piece)
    n_mask = float(opaque)
    sh, sw = mask.shape
    if info is not None:
        info["template_sq"] = template_sq
        info["opaque"] = opaque
        info["piece_width"] = int(sw)
        info["piece_height"] = int(sh)
        info["background_width"] = int(bg_luma.shape[1])
        info["background_height"] = int(bg_luma.shape[0])
        info["ncc"] = 0.0
        info["ncc_edge"] = 0.0
        info["reason"] = ""

    height, width = bg_luma.shape
    pad_y, pad_x = sh, sw

    def pad_edge(arr: np.ndarray) -> np.ndarray:
        return np.pad(arr, ((pad_y, pad_y), (pad_x, pad_x)), mode="edge")

    def pad_zero(arr: np.ndarray) -> np.ndarray:
        return np.pad(
            arr, ((pad_y, pad_y), (pad_x, pad_x)), mode="constant", constant_values=0.0
        )

    bg_p = pad_edge(bg_luma)
    on_p = pad_zero(np.ones_like(bg_luma))

    if template_sq < FLAT_TEMPLATE_SQ:
        if info is not None:
            info["reason"] = "flat"
        if log:
            sys.stderr.write(
                "[puzzle] piece interior has no luminance variance; no target\n"
            )
        return None

    sum_i = _fft_correlate(bg_p, luma_mask)
    sum_i2 = _fft_correlate(bg_p * bg_p, luma_mask)
    sum_t = _fft_correlate(bg_p, template)
    sum_on = _fft_correlate(on_p, mask)
    on_luma = _fft_correlate(on_p, luma_mask)
    on_safe = np.maximum(on_luma, 1.0)
    imean = sum_i / on_safe
    var_i = np.maximum(sum_i2 - imean * imean * on_safe, 1e-3)
    ncc = np.clip(sum_t / np.sqrt(template_sq * var_i), -1.0, 1.0)

    bg_edges = pad_edge(_sobel(bg_luma))
    piece_edges = _sobel(piece_luma) * mask
    edge_mean = float((piece_edges * mask).sum() / n_mask)
    edge_t = (piece_edges - edge_mean) * mask
    edge_sq = float((edge_t * edge_t).sum()) or 1.0
    on_edge = np.maximum(sum_on, 1.0)
    sum_e = _fft_correlate(bg_edges, mask)
    sum_e2 = _fft_correlate(bg_edges * bg_edges, mask)
    sum_te = _fft_correlate(bg_edges, edge_t)
    emean = sum_e / on_edge
    var_e = np.maximum(sum_e2 - emean * emean * on_edge, 1e-3)
    ncc_e = np.clip(sum_te / np.sqrt(edge_sq * var_e), -1.0, 1.0)

    # Darkness uses the full silhouette: the real cut is the deepest region
    # even when overlapping decoys locally compete.
    sum_full = _fft_correlate(bg_p, mask)
    darkness = 1.0 - (sum_full / np.maximum(sum_on, 1.0) / 255.0)
    on_frac = sum_on / n_mask
    score = 0.35 * ncc + 0.45 * ncc_e + 0.20 * darkness
    score = np.where(on_frac >= 0.88, score, -1e9)

    ys, xs = np.indices(score.shape)
    cx = xs + sw / 2.0 - pad_x
    cy = ys + sh / 2.0 - pad_y
    if exclude_radius is None:
        exclude_radius = 0.45 * max(sw, sh)
    dist_origin = np.hypot(cx - origin[0], cy - origin[1])
    valid = (
        (dist_origin >= exclude_radius)
        & (cx >= 0)
        & (cx <= width)
        & (cy >= 0)
        & (cy <= height)
    )
    score_v = np.where(valid, score, -1e9)
    peak = int(np.argmax(score_v))
    iy, ix = np.unravel_index(peak, score.shape)
    if float(score_v[iy, ix]) < -1e8:
        return None
    ncc_val = float(ncc[iy, ix])
    ncc_e_val = float(ncc_e[iy, ix])
    if info is not None:
        info["ncc"] = ncc_val
        info["ncc_edge"] = ncc_e_val
    # Saturated luminance NCC with a weak edge is the binary-mask /
    # dull-window peak, not the cut. Do not let it win the argmax.
    if ncc_val >= SATURATED_LUMA_NCC and ncc_e_val < WEAK_EDGE_NCC:
        if info is not None:
            info["reason"] = "saturated"
        if log:
            sys.stderr.write(
                "[puzzle] saturated luminance ncc with weak edges; no target "
                f"(ncc={ncc_val:.3f} edge={ncc_e_val:.3f})\n"
            )
        return None
    return PuzzleMatch(
        x=float(cx[iy, ix]),
        y=float(cy[iy, ix]),
        score=float(score[iy, ix]),
        ncc=ncc_val,
        ncc_edge=ncc_e_val,
        darkness=float(darkness[iy, ix]),
    )


def human_drag_path(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    rng: random.Random | None = None,
) -> list[tuple[float, float]]:
    """Dense curved path from the overlay centre to the matched cut."""
    rng = rng or random.Random()
    dx = x1 - x0
    dy = y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 1:
        return [(x0, y0), (x1, y1)]
    nx, ny = -dy / dist, dx / dist
    bulge = rng.uniform(0.12, 0.32) * dist
    if rng.random() < 0.5:
        bulge = -bulge
    c1 = (
        x0 + dx * 0.28 + nx * bulge * 0.55,
        y0 + dy * 0.28 + ny * bulge * 0.55,
    )
    c2 = (
        x0 + dx * 0.72 + nx * bulge,
        y0 + dy * 0.72 + ny * bulge,
    )
    steps = max(28, int(dist * 1.2) + rng.randint(6, 18))
    points: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        u = 1.0 - t
        x = (
            u**3 * x0
            + 3 * u**2 * t * c1[0]
            + 3 * u * t**2 * c2[0]
            + t**3 * x1
        )
        y = (
            u**3 * y0
            + 3 * u**2 * t * c1[1]
            + 3 * u * t**2 * c2[1]
            + t**3 * y1
        )
        if 0 < i < steps and rng.random() < 0.18:
            x += rng.uniform(-0.7, 0.7)
            y += rng.uniform(-0.7, 0.7)
        points.append((x, y))
    points[0] = (x0, y0)
    points[-1] = (x1, y1)
    return points


def board_rect_from_state(state: dict[str, Any]) -> BoardRect | None:
    board = state.get("board")
    if not isinstance(board, dict):
        return None
    try:
        rect = BoardRect(
            left=float(board["left"]),
            top=float(board["top"]),
            width=float(board["width"]),
            height=float(board["height"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if rect.width < 8 or rect.height < 8:
        return None
    return rect
