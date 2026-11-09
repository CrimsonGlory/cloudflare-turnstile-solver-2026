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
import io
import math
import random
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

BOARD_WIDTH = 300
BOARD_HEIGHT = 200


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


def match_piece(
    background: np.ndarray,
    piece: np.ndarray,
    origin: tuple[float, float],
    *,
    exclude_radius: float | None = None,
) -> PuzzleMatch:
    """Locate the darkened cut that matches ``piece``.

    ``origin`` is the current centre of the overlay in board pixels, and is
    excluded so a leftover composite cannot win. Returned ``x/y`` are the
    centre the widget should be dragged to (``finalX`` / ``finalY``).
    """
    bg_luma = _luma(background)
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

    # The overlay rim is a luminance step of up to ~72; the hole has a dark
    # inner shadow in the same band. Drop that ring from the intensity NCC
    # so the interior (including overlap lines) can lock on.
    sh, sw = mask.shape
    interior = _erode(mask, max(2, int(round(0.07 * max(sh, sw)))))
    n_interior = float(interior.sum())
    luma_mask = interior if n_interior >= 20 else mask
    n_luma = float(luma_mask.sum()) or n_mask

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

    tmean = float((piece_luma * luma_mask).sum() / n_luma)
    template = (piece_luma - tmean) * luma_mask
    template_sq = float((template * template).sum())
    if template_sq < 1e-3:
        template = luma_mask
        template_sq = float((template * template).sum())

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
    return PuzzleMatch(
        x=float(cx[iy, ix]),
        y=float(cy[iy, ix]),
        score=float(score[iy, ix]),
        ncc=float(ncc[iy, ix]),
        ncc_edge=float(ncc_e[iy, ix]),
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
