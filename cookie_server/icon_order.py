"""Match Prosopo icon-order legend chips onto the 300x200 board.

Each legend chip is an upright glyph; the same glyph is painted on the board
rotated and size-jittered, keeping its hue. The provider grades click i as a
hit on target i when the Euclidean distance is at most ``tolerance * size``
(~28px at the default size of 38). Clicks are in board pixels.

Icons are the only thin, saturated strokes with a dark halo, so a saturation
x value-edge map finds the seven (target+decoy) blobs. Each legend chip is
then assigned to the unused blob whose hue is closest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from cookie_server.puzzle import _sobel, decode_image

BOARD_WIDTH = 300
BOARD_HEIGHT = 200
LEGEND_ICON_SIZE = 26
LEGEND_GAP = 6
DEFAULT_ICON_SIZE = 38.0
DEFAULT_TOLERANCE = 0.75
MIN_SEPARATION = 1.15 * DEFAULT_ICON_SIZE


@dataclass(frozen=True)
class IconHit:
    x: float
    y: float
    score: float
    hue: float


@dataclass(frozen=True)
class _Blob:
    x: float
    y: float
    hue: float
    mass: float


def split_legend(
    legend: np.ndarray,
    icon_size: int | None = None,
) -> list[np.ndarray]:
    """Slice the legend strip into square chips (size x size, gap 6)."""
    if legend.ndim == 2:
        legend = np.stack([legend] * 3, axis=-1)
    height = int(legend.shape[0])
    width = int(legend.shape[1])
    size = int(icon_size or height or LEGEND_ICON_SIZE)
    if size < 8 or width < size:
        return []
    step = size + LEGEND_GAP
    chips: list[np.ndarray] = []
    x = 0
    while x + size <= width + 1:
        chip = legend[:, x : x + size]
        if chip.shape[1] < size:
            pad = np.zeros(
                (chip.shape[0], size - chip.shape[1], chip.shape[2]),
                dtype=legend.dtype,
            )
            chip = np.concatenate([chip, pad], axis=1)
        chips.append(chip)
        x += step
    return chips


def _rgb01(pixels: np.ndarray) -> np.ndarray:
    rgb = pixels[..., :3].astype(np.float32)
    if rgb.max() > 1.5:
        rgb = rgb / 255.0
    return np.clip(rgb, 0.0, 1.0)


def rgb_to_hsv(pixels: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorised RGB in 0..1 -> hue in degrees, saturation, value."""
    rgb = _rgb01(pixels)
    red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    maxc = np.maximum(np.maximum(red, green), blue)
    minc = np.minimum(np.minimum(red, green), blue)
    delta = maxc - minc
    hue = np.zeros_like(maxc)
    sat = np.where(maxc > 1e-6, delta / np.maximum(maxc, 1e-6), 0.0)
    nz = delta > 1e-6
    delta_safe = np.where(nz, delta, 1.0)
    mask_r = nz & (maxc == red)
    mask_g = nz & (maxc == green)
    mask_b = nz & (maxc == blue)
    hue = np.where(mask_r, (60.0 * ((green - blue) / delta_safe) + 360.0) % 360.0, hue)
    hue = np.where(mask_g, (60.0 * ((blue - red) / delta_safe) + 120.0) % 360.0, hue)
    hue = np.where(mask_b, (60.0 * ((red - green) / delta_safe) + 240.0) % 360.0, hue)
    return hue, sat, maxc


def circular_mean_deg(values: np.ndarray, weights: np.ndarray | None = None) -> float:
    if values.size == 0:
        return 0.0
    rad = np.deg2rad(values.astype(np.float64))
    if weights is None:
        weights = np.ones_like(rad)
    weights = weights.astype(np.float64)
    total = float(weights.sum())
    if total <= 0:
        return 0.0
    mean_x = float((np.cos(rad) * weights).sum() / total)
    mean_y = float((np.sin(rad) * weights).sum() / total)
    return float((np.rad2deg(np.arctan2(mean_y, mean_x)) + 360.0) % 360.0)


def hue_distance(a: np.ndarray | float, b: np.ndarray | float) -> np.ndarray | float:
    diff = np.abs(np.asarray(a, dtype=np.float32) - np.asarray(b, dtype=np.float32))
    return np.minimum(diff, 360.0 - diff)


def chip_hue(chip: np.ndarray) -> float:
    """Hue of the saturated glyph stroke, ignoring the dark chip disc."""
    hue, sat, val = rgb_to_hsv(chip)
    weights = sat * (val > 0.28).astype(np.float32) * (sat > 0.35).astype(np.float32)
    if float(weights.sum()) < 4:
        weights = sat * val
    return circular_mean_deg(hue.reshape(-1), weights.reshape(-1))


def _box_blur_1d(arr: np.ndarray, radius: int, axis: int) -> np.ndarray:
    kernel = 2 * radius + 1
    pad_width = [(0, 0)] * arr.ndim
    pad_width[axis] = (radius, radius)
    padded = np.pad(arr, pad_width, mode="edge")
    moved = np.moveaxis(padded, axis, -1)
    csum = np.cumsum(moved, axis=-1, dtype=np.float64)
    zeros = np.zeros(moved.shape[:-1] + (1,), dtype=np.float64)
    csum = np.concatenate([zeros, csum], axis=-1)
    out = (csum[..., kernel:] - csum[..., :-kernel]) / kernel
    return np.moveaxis(out, -1, axis).astype(np.float32)


def _blur(image: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return image.astype(np.float32)
    blurred = _box_blur_1d(image.astype(np.float32), radius, 1)
    return _box_blur_1d(blurred, radius, 0)


def _stroke_map(board: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    hue, sat, val = rgb_to_hsv(board)
    edges = _sobel(val)
    feat = ((sat > 0.35) & (edges > 0.18)).astype(np.float32)
    return feat, hue, sat, val


def _blobs(board: np.ndarray) -> list[_Blob]:
    feat, hue, sat, val = _stroke_map(board)
    heat = _blur(feat, 5)
    height, width = heat.shape
    ys, xs = np.indices(heat.shape)
    work = heat.copy()
    blobs: list[_Blob] = []
    for _ in range(10):
        peak = int(np.argmax(work))
        py, px = np.unravel_index(peak, work.shape)
        score = float(work[py, px])
        if score < 0.12:
            break
        nearby = np.hypot(xs - px, ys - py) <= 18
        mass = feat * nearby.astype(np.float32)
        total = float(mass.sum())
        work[np.hypot(xs - px, ys - py) < MIN_SEPARATION] = -1.0
        if total < 20:
            continue
        x = float((xs * mass).sum() / total)
        y = float((ys * mass).sum() / total)
        selected = mass > 0
        blob_hue = circular_mean_deg(hue[selected], mass[selected])
        blobs.append(_Blob(x=x, y=y, hue=blob_hue, mass=total))
    return blobs


def _assign(chip_hues: Sequence[float], blobs: Sequence[_Blob], max_hue: float = 42.0) -> list[int] | None:
    """Minimum-cost assignment of chips to distinct blobs by hue distance."""
    n = len(chip_hues)
    if n == 0 or len(blobs) < n:
        return None
    best: tuple[float, list[int]] | None = None

    def rec(index: int, used: set[int], cost: float, picks: list[int]) -> None:
        nonlocal best
        if best is not None and cost >= best[0]:
            return
        if index == n:
            best = (cost, list(picks))
            return
        target = chip_hues[index]
        ranked = sorted(
            (
                (float(hue_distance(target, blob.hue)), j)
                for j, blob in enumerate(blobs)
                if j not in used
            )
        )
        for dist, j in ranked:
            if dist > max_hue:
                break
            rec(index + 1, used | {j}, cost + dist, picks + [j])

    rec(0, set(), 0.0, [])
    if best is None:
        return None
    return best[1]


def match_icon_order(
    board: np.ndarray,
    legend: np.ndarray,
    *,
    legend_icon_size: int | None = None,
) -> list[IconHit]:
    chips = split_legend(legend, legend_icon_size)
    if not chips:
        return []
    hues = [chip_hue(chip) for chip in chips]
    blobs = _blobs(board)
    order = _assign(hues, blobs)
    if order is None:
        order = []
        used: set[int] = set()
        for target in hues:
            best_j = None
            best_d = 1e9
            for j, blob in enumerate(blobs):
                if j in used:
                    continue
                dist = float(hue_distance(target, blob.hue))
                if dist < best_d:
                    best_d = dist
                    best_j = j
            if best_j is None or best_d > 50:
                continue
            used.add(best_j)
            order.append(best_j)
        if len(order) != len(chips):
            return []
    hits: list[IconHit] = []
    for target, index in zip(hues, order):
        blob = blobs[index]
        dist = float(hue_distance(target, blob.hue))
        hits.append(
            IconHit(
                x=blob.x,
                y=blob.y,
                score=max(0.0, 1.0 - dist / 90.0),
                hue=blob.hue,
            )
        )
    return hits


def decode_icon_images(
    board_data: str | bytes,
    legend_data: str | bytes,
) -> tuple[np.ndarray, np.ndarray]:
    board = decode_image(board_data)
    legend = decode_image(legend_data)
    return board, legend
