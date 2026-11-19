#!/usr/bin/env python3
"""Prosopo puzzle matching and CDP drag."""

from __future__ import annotations

import asyncio
import base64
import io
import math
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cookie_server import cdp_browser as cdp_mod  # noqa: E402
from cookie_server.cdp_browser import (  # noqa: E402
    CdpBrowser,
    _PROSOPO_PUZZLE_EXTRACT_JS,
    procaptcha_token,
    wait_for_cdp,
)
from cookie_server.puzzle import (  # noqa: E402
    BOARD_HEIGHT,
    BOARD_WIDTH,
    SATURATED_LUMA_NCC,
    WEAK_EDGE_NCC,
    board_rect_from_state,
    crop_css_rect,
    decode_image,
    human_drag_path,
    match_piece,
    rasterize_css_layer,
    rasterize_piece_spec,
    scale_to_board,
)

CHROME_BIN = (
    os.environ.get("CHROME_BIN")
    or shutil.which("google-chrome-stable")
    or shutil.which("google-chrome")
)
XVFB_BIN = shutil.which("Xvfb")
FIXTURE_DIR = ROOT / "tests" / "fixtures"


def _png_data_uri(pixels: np.ndarray) -> str:
    image = Image.fromarray(pixels)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _png_b64(pixels: np.ndarray) -> str:
    image = Image.fromarray(pixels)
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _as_rgba(pixels: np.ndarray) -> np.ndarray:
    if pixels.ndim == 2:
        return np.stack(
            [pixels, pixels, pixels, np.full_like(pixels, 255)], axis=-1
        ).astype(np.uint8)
    if pixels.shape[2] == 3:
        alpha = np.full(pixels.shape[:2] + (1,), 255, dtype=np.uint8)
        return np.concatenate([pixels, alpha], axis=2)
    return pixels.astype(np.uint8)


def _viewport_screenshot(
    board: np.ndarray,
    *,
    left: float = 100.0,
    top: float = 80.0,
    canvas: tuple[int, int] = (800, 600),
    dpr: float = 1.0,
) -> np.ndarray:
    rgba = _as_rgba(board)
    cw, ch = canvas
    view = np.zeros((int(round(ch * dpr)), int(round(cw * dpr)), 4), dtype=np.uint8)
    view[:, :, 3] = 255
    img = Image.fromarray(rgba)
    scaled = img.resize(
        (int(round(rgba.shape[1] * dpr)), int(round(rgba.shape[0] * dpr))),
        Image.Resampling.BILINEAR,
    )
    arr = np.array(scaled.convert("RGBA"))
    x0 = int(round(left * dpr))
    y0 = int(round(top * dpr))
    h, w = arr.shape[:2]
    view[y0 : y0 + h, x0 : x0 + w] = arr
    return view


def make_clean_photo_pair() -> dict[str, Any]:
    """Smooth photo with no darkened cut: live failure mode (ncc=1, weak edge)."""
    rng = np.random.default_rng(0)
    background = np.full((BOARD_HEIGHT, BOARD_WIDTH, 3), 110, dtype=np.float32)
    yy, xx = np.mgrid[0:BOARD_HEIGHT, 0:BOARD_WIDTH]
    background[:, :, 0] += 8 * np.sin(xx / 40.0)
    background[:, :, 1] += 6 * np.cos(yy / 35.0)
    background[:, :, 2] += 5 * np.sin((xx + yy) / 50.0)
    background += rng.normal(0, 0.4, background.shape)
    background = np.clip(background, 0, 255).astype(np.uint8)
    size = 56
    origin = (30.0 + size / 2.0, 40.0 + size / 2.0)
    piece = np.zeros((size, size, 4), dtype=np.uint8)
    piece[:, :, :3] = background[40 : 40 + size, 30 : 30 + size]
    piece[:, :, 3] = 255
    piece[:, :, :3] = np.clip(piece[:, :, :3].astype(np.int16) + 6, 0, 255).astype(
        np.uint8
    )
    return {
        "background": background,
        "piece": piece,
        "origin": origin,
        "piece_size": size,
    }


def _wire_puzzle_browser(
    browser: CdpBrowser,
    *,
    extract: dict[str, Any],
    screenshot: np.ndarray,
    snapshot_fn: Callable[[], dict[str, Any]],
    calls: list[str] | None = None,
    on_mouse: Callable[[str, dict | None], None] | None = None,
) -> None:
    shot_b64 = _png_b64(screenshot)

    async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
        if calls is not None:
            calls.append(method)
        if on_mouse and method == "Input.dispatchMouseEvent":
            on_mouse(method, params)
        if method == "Page.captureScreenshot":
            return {"data": shot_b64}
        return {}

    async def evaluate(expression: str, session_id: str):
        if "devicePixelRatio" in expression:
            return 1
        if "procaptchaPuzzleHide" in expression or "procaptchaPuzzleRestore" in expression:
            return {"ok": True}
        if "toDataURL" in expression or "procaptchaPuzzleExtract" in expression:
            return extract
        return snapshot_fn()

    browser.call = call  # type: ignore[method-assign]
    browser.evaluate = evaluate  # type: ignore[method-assign]


def _rounded_box_distance(px: float, py: float, half: float, radius: float) -> float:
    qx = abs(px) - half + radius
    qy = abs(py) - half + radius
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - radius


def _coverage_from_distance(distance: float) -> float:
    t = 0.5 - distance
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0
    return t * t * (3 - 2 * t)


def _jigsaw_coverage(size: int, rng: random.Random) -> np.ndarray:
    knob_radius = size * rng.uniform(0.13, 0.17)
    body_half = size / 2 - knob_radius * 1.05
    corner_radius = body_half * rng.uniform(0.14, 0.3)
    sides = [0, 1, 2, 3]
    rng.shuffle(sides)
    count = rng.randint(2, 3)
    edges = ["flat"] * 4
    for side in sides[:count]:
        edges[side] = "tab" if rng.random() < 0.55 else "blank"
    if "tab" not in edges:
        edges[sides[0]] = "tab"
    axes = {
        0: ((0, -1), (1, 0)),
        1: ((1, 0), (0, 1)),
        2: ((0, 1), (-1, 0)),
        3: ((-1, 0), (0, -1)),
    }
    knobs = []
    for side, kind in enumerate(edges):
        if kind == "flat":
            continue
        normal, along = axes[side]
        radius = knob_radius * rng.uniform(0.88, 1.12)
        offset = radius * 0.52 if kind == "tab" else -radius * 0.42
        slide_limit = max(0.0, body_half - radius - corner_radius * 0.6)
        slide = rng.uniform(-slide_limit, slide_limit) * 0.6
        knobs.append(
            (
                kind,
                normal[0] * (body_half + offset) + along[0] * slide,
                normal[1] * (body_half + offset) + along[1] * slide,
                radius,
            )
        )
    max_extent = body_half
    for kind, cx, cy, radius in knobs:
        if kind == "tab":
            max_extent = max(max_extent, abs(cx) + radius, abs(cy) + radius)
    limit = size / 2 - 1.5
    scale = limit / max_extent if max_extent > limit else 1.0
    body_half *= scale
    corner_radius *= scale
    knobs = [(k, cx * scale, cy * scale, r * scale) for k, cx, cy, r in knobs]
    cov = np.zeros((size, size), dtype=np.float32)
    for ly in range(size):
        py = ly - size / 2
        for lx in range(size):
            px = lx - size / 2
            d = _rounded_box_distance(px, py, body_half, corner_radius)
            for kind, cx, cy, radius in knobs:
                if kind == "tab":
                    d = min(d, math.hypot(px - cx, py - cy) - radius)
            for kind, cx, cy, radius in knobs:
                if kind == "blank":
                    d = max(d, -(math.hypot(px - cx, py - cy) - radius))
            cov[ly, lx] = _coverage_from_distance(d)
    return cov


def _paste(board: np.ndarray, cov: np.ndarray, cx: float, cy: float, fn) -> None:
    size = cov.shape[0]
    left = int(round(cx - size / 2))
    top = int(round(cy - size / 2))
    height, width = board.shape[:2]
    for ly in range(size):
        y = top + ly
        if y < 0 or y >= height:
            continue
        for lx in range(size):
            x = left + lx
            if x < 0 or x >= width:
                continue
            alpha = float(cov[ly, lx])
            if alpha <= 0:
                continue
            fn(board, x, y, alpha, lx, ly)


def make_synthetic_puzzle(seed: int = 0) -> dict[str, Any]:
    rng = random.Random(seed)
    height, width = BOARD_HEIGHT, BOARD_WIDTH
    c1 = np.array(
        [rng.randint(40, 180), rng.randint(80, 200), rng.randint(80, 200)],
        dtype=np.float32,
    )
    c2 = np.array(
        [rng.randint(40, 180), rng.randint(80, 200), rng.randint(80, 200)],
        dtype=np.float32,
    )
    yy, xx = np.mgrid[0:height, 0:width]
    t = (xx / (width - 1) * 0.6 + yy / (height - 1) * 0.4).astype(np.float32)
    background = c1 * (1 - t)[..., None] + c2 * t[..., None]
    background += np.random.default_rng(seed + 99).uniform(-5, 5, background.shape)
    np.clip(background, 0, 255, out=background)

    piece_size = rng.choice([44, 56, 70, 84, 97])
    target = (float(rng.randint(150, 280)), float(rng.randint(30, 170)))
    origin = (float(rng.randint(20, 130)), float(rng.randint(30, 170)))
    while math.hypot(origin[0] - target[0], origin[1] - target[1]) < piece_size:
        origin = (float(rng.randint(20, 130)), float(rng.randint(30, 170)))

    for _ in range(rng.randint(4, 7)):
        cov = _jigsaw_coverage(piece_size, rng)
        dcx = rng.uniform(20, width - 20)
        dcy = rng.uniform(20, height - 20)

        def decoy_fn(board, x, y, alpha, lx, ly, cov=cov):
            edge = 1.0 if cov[ly, lx] < 0.85 else 0.0
            factor = 0.70 * (1 - 0.35 * edge)
            board[y, x] = board[y, x] * (1 - alpha) + board[y, x] * factor * alpha
            board[y, x] -= np.array([20.0, 20.0, 20.0]) * alpha * edge

        _paste(background, cov, dcx, dcy, decoy_fn)

    cov = _jigsaw_coverage(piece_size, rng)
    left = int(round(target[0] - piece_size / 2))
    top = int(round(target[1] - piece_size / 2))
    piece = np.zeros((piece_size, piece_size, 4), dtype=np.float32)
    brightness = rng.uniform(-8, 8)
    for ly in range(piece_size):
        y = top + ly
        for lx in range(piece_size):
            x = left + lx
            alpha = float(cov[ly, lx])
            if alpha <= 0:
                continue
            if 0 <= x < width and 0 <= y < height:
                rgb = background[y, x].copy()
            else:
                continue
            noise = rng.uniform(-7, 7)
            # Real renderer uses a ~6.5% rim with a 24–72 luminance step.
            rim = 40.0 if 0 < alpha < 0.92 else 0.0
            piece[ly, lx, 0:3] = np.clip(rgb + brightness + noise + rim, 0, 255)
            piece[ly, lx, 3] = alpha * 255

    def hole_fn(board, x, y, alpha, lx, ly, cov=cov):
        edge = 1.0 if cov[ly, lx] < 0.85 else 0.0
        factor = 0.55 * (1 - 0.45 * edge)
        board[y, x] = board[y, x] * (1 - alpha) + board[y, x] * factor * alpha

    _paste(background, cov, target[0], target[1], hole_fn)
    np.clip(background, 0, 255, out=background)
    return {
        "background": background.astype(np.uint8),
        "piece": piece.astype(np.uint8),
        "origin": origin,
        "target": target,
        "piece_size": piece_size,
    }


def _snapshot(
    *,
    ready: str = "complete",
    href: str = "https://example.com/",
    html: str = "<html><body><div class='procaptcha'></div></body></html>",
    procaptcha: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ready": ready,
        "title": "Example",
        "href": href,
        "html": html,
    }
    if procaptcha is not None:
        payload["procaptcha"] = procaptcha
    return payload


class PuzzleMatchTest(unittest.TestCase):
    def test_match_recovers_cut_on_synthetic_puzzles(self) -> None:
        errors = []
        for seed in range(12):
            puzzle = make_synthetic_puzzle(seed * 11 + 4)
            hit = match_piece(
                puzzle["background"],
                puzzle["piece"],
                puzzle["origin"],
            )
            self.assertIsNotNone(hit, f"seed={seed} returned no target")
            assert hit is not None
            err = math.hypot(hit.x - puzzle["target"][0], hit.y - puzzle["target"][1])
            errors.append(err)
            self.assertLessEqual(
                err,
                8.0,
                f"seed={seed} size={puzzle['piece_size']} "
                f"target={puzzle['target']} hit=({hit.x:.1f},{hit.y:.1f}) "
                f"score={hit.score:.3f}",
            )
        self.assertLessEqual(max(errors), 8.0)
        self.assertLess(sum(errors) / len(errors), 2.0)

    def test_match_piece_still_finds_darkened_cut(self) -> None:
        puzzle = make_synthetic_puzzle(8)
        hit = match_piece(
            puzzle["background"],
            puzzle["piece"],
            puzzle["origin"],
        )
        self.assertIsNotNone(hit)
        assert hit is not None
        err = math.hypot(hit.x - puzzle["target"][0], hit.y - puzzle["target"][1])
        self.assertLessEqual(err, 8.0)
        self.assertFalse(
            hit.ncc >= SATURATED_LUMA_NCC and hit.ncc_edge < WEAK_EDGE_NCC
        )

    def test_flat_piece_does_not_return_saturated_ncc_target(self) -> None:
        background = np.full((BOARD_HEIGHT, BOARD_WIDTH, 3), 120, dtype=np.uint8)
        background[30:90, 160:230] = 40
        piece = np.zeros((48, 48, 4), dtype=np.uint8)
        piece[:, :, 0:3] = 80
        piece[:, :, 3] = 255
        hit = match_piece(background, piece, (50.0, 50.0))
        self.assertTrue(
            hit is None
            or not (hit.ncc >= SATURATED_LUMA_NCC and hit.ncc_edge < WEAK_EDGE_NCC),
            None
            if hit is None
            else f"ncc={hit.ncc:.3f} edge={hit.ncc_edge:.3f} score={hit.score:.3f}",
        )

    def test_clean_photo_without_cut_is_rejected(self) -> None:
        pair = make_clean_photo_pair()
        hit = match_piece(pair["background"], pair["piece"], pair["origin"])
        self.assertIsNone(hit)

    def test_painted_board_crop_keeps_overlay_darkening(self) -> None:
        clean = np.zeros((BOARD_HEIGHT, BOARD_WIDTH, 3), dtype=np.uint8)
        yy, xx = np.mgrid[0:BOARD_HEIGHT, 0:BOARD_WIDTH]
        clean[:, :, 0] = (40 + xx * 0.4).astype(np.uint8)
        clean[:, :, 1] = (80 + yy * 0.3).astype(np.uint8)
        clean[:, :, 2] = 160
        painted = clean.copy()
        painted[70:130, 160:230] = (painted[70:130, 160:230].astype(np.float32) * 0.45).astype(
            np.uint8
        )
        shot = _viewport_screenshot(painted, left=40, top=50, dpr=2.0)
        crop = crop_css_rect(shot, 40, 50, 300, 200, dpr=2.0)
        self.assertIsNotNone(crop)
        assert crop is not None
        board = scale_to_board(crop)
        hole = float(board[100, 190, :3].mean())
        tile = float(clean[100, 190].mean())
        self.assertLess(hole, tile - 20)
        self.assertFalse(np.allclose(board[:, :, :3], clean, atol=8))

    def test_decode_roundtrip_preserves_alpha(self) -> None:
        puzzle = make_synthetic_puzzle(1)
        uri = _png_data_uri(puzzle["piece"])
        decoded = decode_image(uri)
        self.assertEqual(decoded.shape[2], 4)
        self.assertGreater(int((decoded[:, :, 3] > 30).sum()), 20)

    def test_drag_path_starts_and_ends_on_the_cut(self) -> None:
        path = human_drag_path(40.0, 80.0, 240.0, 150.0, rng=random.Random(0))
        self.assertGreaterEqual(len(path), 28)
        self.assertEqual(path[0], (40.0, 80.0))
        self.assertEqual(path[-1], (240.0, 150.0))
        length = sum(
            math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1])
            for i in range(1, len(path))
        )
        straight = math.hypot(200.0, 70.0)
        self.assertGreater(length, straight)


class PuzzleRasterizeTest(unittest.TestCase):
    def test_css_background_crop_is_not_a_squashed_full_image(self) -> None:
        full = np.zeros((BOARD_HEIGHT, BOARD_WIDTH, 4), dtype=np.uint8)
        full[:, :, 3] = 255
        full[0:100, 0:150] = [220, 30, 30, 255]
        full[0:100, 150:300] = [30, 200, 40, 255]
        full[100:200, 0:150] = [40, 40, 220, 255]
        full[100:200, 150:300] = [230, 220, 30, 255]
        crop = rasterize_css_layer(
            full,
            60,
            40,
            size="300px 200px",
            position="-150px -100px",
            repeat="no-repeat",
        )
        # Visible face is the yellow quadrant, not a mix of all four.
        pixel = crop[20, 30, :3]
        self.assertGreater(int(pixel[0]), 180)
        self.assertGreater(int(pixel[1]), 170)
        self.assertLess(int(pixel[2]), 80)
        squashed = np.array(
            Image.fromarray(full).resize((60, 40), Image.Resampling.BILINEAR)
        )
        self.assertGreater(
            abs(int(squashed[20, 30, 0]) - int(pixel[0])),
            30,
        )

    def test_css_mask_keeps_transparent_pixels_transparent(self) -> None:
        full = np.full((BOARD_HEIGHT, BOARD_WIDTH, 4), 180, dtype=np.uint8)
        full[:, :, 3] = 255
        mask = np.zeros((40, 60, 4), dtype=np.uint8)
        yy, xx = np.ogrid[:40, :60]
        mask[:, :, 3] = np.where((xx - 30) ** 2 + (yy - 20) ** 2 < 12**2, 255, 0)
        masked = rasterize_css_layer(
            full,
            60,
            40,
            size="300px 200px",
            position="0px 0px",
            repeat="no-repeat",
            mask_image=mask,
            mask_size="60px 40px",
            mask_position="0px 0px",
            mask_repeat="no-repeat",
        )
        self.assertEqual(int(masked[0, 0, 3]), 0)
        self.assertEqual(int(masked[39, 59, 3]), 0)
        self.assertGreater(int(masked[20, 30, 3]), 200)

    def test_clip_path_circle_is_applied_as_alpha(self) -> None:
        full = np.full((80, 80, 4), 200, dtype=np.uint8)
        full[:, :, 3] = 255
        clipped = rasterize_css_layer(
            full,
            80,
            80,
            size="80px 80px",
            position="0px 0px",
            clip_path="circle(20px at 50% 50%)",
        )
        self.assertEqual(int(clipped[0, 0, 3]), 0)
        self.assertGreater(int(clipped[40, 40, 3]), 200)

    def test_piece_spec_uses_background_position_crop(self) -> None:
        full = np.zeros((BOARD_HEIGHT, BOARD_WIDTH, 4), dtype=np.uint8)
        full[:, :, 3] = 255
        full[80:120, 150:210] = [12, 240, 18, 255]
        spec = {
            "image": _png_data_uri(full),
            "boxWidth": 60,
            "boxHeight": 40,
            "backgroundSize": "300px 200px",
            "backgroundPosition": "-150px -80px",
            "backgroundRepeat": "no-repeat",
        }
        layer = rasterize_piece_spec(spec)
        pixel = layer[20, 30, :3]
        self.assertLess(int(pixel[0]), 40)
        self.assertGreater(int(pixel[1]), 200)


class PuzzleClearanceTest(unittest.TestCase):
    def test_drag_path_presses_moves_and_releases(self) -> None:
        calls: list[tuple[str, dict | None]] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                calls.append((method, params))
                return {}

            browser.call = call  # type: ignore[method-assign]
            dragged = await browser._drag_path(
                "session",
                [(10.0, 10.0), (12.0, 11.0), (40.0, 30.0)],
                step_delay=0.0,
            )
            self.assertTrue(dragged)

        asyncio.run(run())
        types = [(params or {}).get("type") for _method, params in calls]
        self.assertEqual(types[0], "mouseMoved")
        self.assertEqual(types[1], "mousePressed")
        self.assertIn("mouseMoved", types[2:-1])
        self.assertEqual(types[-1], "mouseReleased")
        self.assertEqual(calls[1][1]["buttons"], 1)
        self.assertEqual(calls[2][1]["buttons"], 1)
        self.assertEqual(calls[-1][1]["x"], 40.0)
        self.assertEqual(calls[-1][1]["y"], 30.0)
        for _method, params in calls:
            self.assertEqual((params or {}).get("pointerType"), "mouse")
        move_buttons = [
            (params or {}).get("buttons")
            for _method, params in calls[2:-1]
        ]
        self.assertTrue(move_buttons)
        self.assertTrue(all(buttons == 1 for buttons in move_buttons))

    def test_clearance_drags_puzzle_then_returns_token(self) -> None:
        puzzle = make_synthetic_puzzle(8)
        extract = {
            "ready": True,
            "originX": puzzle["origin"][0],
            "originY": puzzle["origin"][1],
            "pieceSize": puzzle["piece_size"],
            "board": {"left": 100.0, "top": 80.0, "width": 300.0, "height": 200.0},
            "background": _png_data_uri(puzzle["background"]),
            "piece": _png_data_uri(puzzle["piece"]),
        }
        released = {"n": 0}
        calls: list[str] = []
        screenshot = _viewport_screenshot(puzzle["background"])

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            def on_mouse(_method: str, params: dict | None) -> None:
                if (params or {}).get("type") == "mouseReleased":
                    released["n"] += 1

            def snapshot_fn() -> dict[str, Any]:
                if released["n"]:
                    return _snapshot(
                        procaptcha={
                            "present": True,
                            "solved": True,
                            "kind": "solved",
                            "token": "puzzle-token",
                        }
                    )
                return _snapshot(
                    procaptcha={
                        "present": True,
                        "solved": False,
                        "kind": "puzzle",
                        "originX": puzzle["origin"][0],
                        "originY": puzzle["origin"][1],
                        "pieceSize": puzzle["piece_size"],
                        "board": extract["board"],
                    }
                )

            _wire_puzzle_browser(
                browser,
                extract=extract,
                screenshot=screenshot,
                snapshot_fn=snapshot_fn,
                calls=calls,
                on_mouse=on_mouse,
            )
            snapshot, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=8.0
            )
            self.assertFalse(timed_out)
            self.assertEqual(procaptcha_token(snapshot), "puzzle-token")

        asyncio.run(run())
        self.assertGreaterEqual(released["n"], 1)
        self.assertGreater(calls.count("Input.dispatchMouseEvent"), 4)
        self.assertIn("Page.captureScreenshot", calls)
        self.assertNotIn("Page.reload", calls)

    def test_unknown_visual_challenge_is_still_unsolved(self) -> None:
        calls: list[str] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                calls.append(method)
                return {}

            async def evaluate(expression: str, session_id: str):
                return _snapshot(
                    procaptcha={
                        "present": True,
                        "solved": False,
                        "kind": "visual",
                    }
                )

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            _snapshot_out, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=0.2
            )
            self.assertTrue(timed_out)

        asyncio.run(run())
        self.assertNotIn("Input.dispatchMouseEvent", calls)
        self.assertNotIn("Page.reload", calls)

    def test_unmoved_puzzle_drag_is_failure_and_not_retried(self) -> None:
        puzzle = make_synthetic_puzzle(8)
        extract = {
            "ready": True,
            "originX": puzzle["origin"][0],
            "originY": puzzle["origin"][1],
            "pieceSize": puzzle["piece_size"],
            "board": {"left": 100.0, "top": 80.0, "width": 300.0, "height": 200.0},
            "pieceRect": {
                "left": 120.0,
                "top": 90.0,
                "width": float(puzzle["piece_size"]),
                "height": float(puzzle["piece_size"]),
            },
            "background": _png_data_uri(puzzle["background"]),
            "piece": _png_data_uri(puzzle["piece"]),
        }
        pressed = {"n": 0}

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            def on_mouse(_method: str, params: dict | None) -> None:
                if (params or {}).get("type") == "mousePressed":
                    pressed["n"] += 1

            _wire_puzzle_browser(
                browser,
                extract=extract,
                screenshot=_viewport_screenshot(puzzle["background"]),
                snapshot_fn=lambda: _snapshot(
                    procaptcha={
                        "present": True,
                        "solved": False,
                        "kind": "puzzle",
                        "originX": puzzle["origin"][0],
                        "originY": puzzle["origin"][1],
                        "pieceSize": puzzle["piece_size"],
                        "board": extract["board"],
                    }
                ),
                on_mouse=on_mouse,
            )
            _snapshot_out, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=1.5
            )
            self.assertTrue(timed_out)

        asyncio.run(run())
        self.assertEqual(pressed["n"], 1)

    def test_kaleidoscope_tile_src_is_not_fed_to_matcher(self) -> None:
        puzzle = make_synthetic_puzzle(8)
        clean = np.zeros_like(puzzle["background"])
        yy, xx = np.mgrid[0:BOARD_HEIGHT, 0:BOARD_WIDTH]
        clean[:, :, 0] = (50 + xx * 0.2).astype(np.uint8)
        clean[:, :, 1] = (90 + yy * 0.15).astype(np.uint8)
        clean[:, :, 2] = 140
        extract = {
            "ready": True,
            "originX": puzzle["origin"][0],
            "originY": puzzle["origin"][1],
            "pieceSize": puzzle["piece_size"],
            "board": {"left": 100.0, "top": 80.0, "width": 300.0, "height": 200.0},
            "background": _png_data_uri(clean),
            "piece": _png_data_uri(puzzle["piece"]),
        }
        seen: dict[str, Any] = {}
        original = cdp_mod.match_piece

        def wrapped(background, piece, origin, **kwargs):
            seen["bg"] = np.array(background)
            return original(background, piece, origin, **kwargs)

        cdp_mod.match_piece = wrapped  # type: ignore[assignment]
        released = {"n": 0}
        try:

            async def run() -> None:
                browser = CdpBrowser(None, None, focus_windows=False)

                def on_mouse(_method: str, params: dict | None) -> None:
                    if (params or {}).get("type") == "mouseReleased":
                        released["n"] += 1

                def snapshot_fn() -> dict[str, Any]:
                    if released["n"]:
                        return _snapshot(
                            procaptcha={
                                "present": True,
                                "solved": True,
                                "kind": "solved",
                                "token": "puzzle-token",
                            }
                        )
                    return _snapshot(
                        procaptcha={
                            "present": True,
                            "solved": False,
                            "kind": "puzzle",
                            "originX": puzzle["origin"][0],
                            "originY": puzzle["origin"][1],
                            "pieceSize": puzzle["piece_size"],
                            "board": extract["board"],
                        }
                    )

                _wire_puzzle_browser(
                    browser,
                    extract=extract,
                    screenshot=_viewport_screenshot(puzzle["background"]),
                    snapshot_fn=snapshot_fn,
                    on_mouse=on_mouse,
                )
                snapshot, timed_out = await browser._wait_for_page_clearance(
                    "t", "s", timeout=8.0
                )
                self.assertFalse(timed_out)
                self.assertEqual(procaptcha_token(snapshot), "puzzle-token")

            asyncio.run(run())
        finally:
            cdp_mod.match_piece = original  # type: ignore[assignment]
        self.assertIn("bg", seen)
        fed = seen["bg"]
        tx, ty = puzzle["target"]
        hole = float(fed[int(ty), int(tx), :3].mean())
        tile = float(clean[int(ty), int(tx)].mean())
        painted = float(puzzle["background"][int(ty), int(tx)].mean())
        self.assertLess(hole, tile - 8)
        self.assertLess(abs(hole - painted), 18)

    def test_same_raster_hash_logs_saturated_line_once(self) -> None:
        pair = make_clean_photo_pair()
        extract = {
            "ready": True,
            "originX": pair["origin"][0],
            "originY": pair["origin"][1],
            "pieceSize": pair["piece_size"],
            "board": {"left": 100.0, "top": 80.0, "width": 300.0, "height": 200.0},
            "piece": _png_data_uri(pair["piece"]),
        }
        match_calls = {"n": 0}
        original = cdp_mod.match_piece

        def wrapped(background, piece, origin, **kwargs):
            match_calls["n"] += 1
            return original(background, piece, origin, **kwargs)

        cdp_mod.match_piece = wrapped  # type: ignore[assignment]
        buf = io.StringIO()
        old_err = sys.stderr
        sys.stderr = buf
        try:

            async def run() -> None:
                browser = CdpBrowser(None, None, focus_windows=False)
                _wire_puzzle_browser(
                    browser,
                    extract=extract,
                    screenshot=_viewport_screenshot(pair["background"]),
                    snapshot_fn=lambda: _snapshot(
                        procaptcha={
                            "present": True,
                            "solved": False,
                            "kind": "puzzle",
                            "originX": pair["origin"][0],
                            "originY": pair["origin"][1],
                            "pieceSize": pair["piece_size"],
                            "board": extract["board"],
                        }
                    ),
                )
                _snapshot_out, timed_out = await browser._wait_for_page_clearance(
                    "t", "s", timeout=2.2
                )
                self.assertTrue(timed_out)

            asyncio.run(run())
        finally:
            sys.stderr = old_err
            cdp_mod.match_piece = original  # type: ignore[assignment]
        log = buf.getvalue()
        self.assertEqual(log.count("saturated luminance ncc"), 1, log)
        self.assertEqual(match_calls["n"], 1)
        self.assertIn("hash=", log)
        self.assertIn("template_sq=", log)
        self.assertIn("opaque=", log)

    def test_flat_candidate_tries_the_next_visible_piece(self) -> None:
        puzzle = make_synthetic_puzzle(8)
        flat = np.zeros((48, 48, 4), dtype=np.uint8)
        flat[:, :, :3] = 80
        flat[:, :, 3] = 255
        extract = {
            "ready": True,
            "originX": puzzle["origin"][0],
            "originY": puzzle["origin"][1],
            "pieceSize": puzzle["piece_size"],
            "board": {"left": 100.0, "top": 80.0, "width": 300.0, "height": 200.0},
            "piece": _png_data_uri(flat),
            "pieces": [
                {
                    "originX": puzzle["origin"][0],
                    "originY": puzzle["origin"][1],
                    "pieceSize": 48,
                    "piece": _png_data_uri(flat),
                    "pieceRect": {
                        "left": 120.0,
                        "top": 90.0,
                        "width": 48.0,
                        "height": 48.0,
                    },
                },
                {
                    "originX": puzzle["origin"][0],
                    "originY": puzzle["origin"][1],
                    "pieceSize": puzzle["piece_size"],
                    "piece": _png_data_uri(puzzle["piece"]),
                    "pieceRect": {
                        "left": 120.0,
                        "top": 90.0,
                        "width": float(puzzle["piece_size"]),
                        "height": float(puzzle["piece_size"]),
                    },
                },
            ],
        }
        released = {"n": 0}

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            def on_mouse(_method: str, params: dict | None) -> None:
                if (params or {}).get("type") == "mouseReleased":
                    released["n"] += 1

            def snapshot_fn() -> dict[str, Any]:
                if released["n"]:
                    return _snapshot(
                        procaptcha={
                            "present": True,
                            "solved": True,
                            "kind": "solved",
                            "token": "puzzle-token",
                        }
                    )
                return _snapshot(
                    procaptcha={
                        "present": True,
                        "solved": False,
                        "kind": "puzzle",
                        "originX": puzzle["origin"][0],
                        "originY": puzzle["origin"][1],
                        "pieceSize": puzzle["piece_size"],
                        "board": extract["board"],
                    }
                )

            _wire_puzzle_browser(
                browser,
                extract=extract,
                screenshot=_viewport_screenshot(puzzle["background"]),
                snapshot_fn=snapshot_fn,
                on_mouse=on_mouse,
            )
            snapshot, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=8.0
            )
            self.assertFalse(timed_out)
            self.assertEqual(procaptcha_token(snapshot), "puzzle-token")

        asyncio.run(run())
        self.assertGreaterEqual(released["n"], 1)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _FixtureHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURE_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        return


@unittest.skipUnless(CHROME_BIN and XVFB_BIN, "headed Chrome + Xvfb required")
class PuzzleExtractLiveTest(unittest.TestCase):
    def test_extract_uses_painted_board_and_css_crop(self) -> None:
        record = asyncio.run(self._run())
        self.assertTrue(record["ready"], record)
        self.assertGreater(record["overlay_drop"], 20, record)
        self.assertGreater(record["piece_g"], 150, record)
        self.assertLess(record["piece_b"], 90, record)
        self.assertEqual(record["corner_alpha"], 0, record)
        self.assertGreater(record["center_alpha"], 200, record)
        self.assertFalse(record["used_tile_src"], record)

    async def _run(self) -> dict[str, Any]:
        display = os.environ.get("PROSOPO_PUZZLE_DISPLAY", ":98")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        httpd.daemon_threads = True
        http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        http_thread.start()
        fixture_url = f"http://127.0.0.1:{httpd.server_address[1]}/procaptcha_puzzle_extract.html"
        xvfb = subprocess.Popen(
            [
                XVFB_BIN,
                display,
                "-screen",
                "0",
                "1280x720x24",
                "-ac",
                "+extension",
                "RANDR",
                "-nolisten",
                "tcp",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        chrome: subprocess.Popen | None = None
        browser: CdpBrowser | None = None
        tmp = tempfile.TemporaryDirectory(prefix="procaptcha-puzzle-")
        try:
            self._wait_display(display)
            env = os.environ.copy()
            env["DISPLAY"] = display
            env["HOME"] = tmp.name
            port = _free_port()
            profile = Path(tmp.name) / "chrome"
            profile.mkdir()
            chrome = subprocess.Popen(
                [
                    CHROME_BIN,
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--remote-debugging-address=127.0.0.1",
                    f"--remote-debugging-port={port}",
                    "--remote-allow-origins=*",
                    f"--user-data-dir={profile}",
                    "--window-size=900,700",
                    "about:blank",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await wait_for_cdp(f"http://127.0.0.1:{port}", timeout=20.0)
            browser = await CdpBrowser.connect(
                f"http://127.0.0.1:{port}",
                focus_windows=False,
            )
            created = await browser.call("Target.createTarget", {"url": "about:blank"})
            session = (
                await browser.call(
                    "Target.attachToTarget",
                    {"targetId": created["targetId"], "flatten": True},
                )
            )["sessionId"]
            await browser.call("Page.enable", session_id=session)
            await browser.call("Runtime.enable", session_id=session)
            nav = await browser.call(
                "Page.navigate",
                {"url": fixture_url},
                session_id=session,
                timeout=15.0,
            )
            if nav.get("errorText"):
                raise RuntimeError(nav["errorText"])
            try:
                await browser.wait_for_event(
                    "Page.loadEventFired", session_id=session, timeout=15.0
                )
            except TimeoutError:
                pass
            await asyncio.sleep(0.3)
            extracted = await browser.evaluate(_PROSOPO_PUZZLE_EXTRACT_JS, session)
            board = board_rect_from_state(extracted or {})
            painted = await browser._capture_painted_board(session, board)
            piece = decode_image(str((extracted or {}).get("piece") or ""))
            tile_luma = (200 + 40 + 30) / 3.0
            hole = float(np.mean(painted[95, 200, :3])) if painted is not None else 255.0
            corner_alpha = int(piece[0, 0, 3])
            center_alpha = int(piece[20, 30, 3])
            pixel = piece[20, 30, :3]
            return {
                "ready": bool(isinstance(extracted, dict) and extracted.get("ready")),
                "overlay_drop": tile_luma - hole,
                "piece_g": int(pixel[1]),
                "piece_b": int(pixel[2]),
                "corner_alpha": corner_alpha,
                "center_alpha": center_alpha,
                "used_tile_src": "background" in (extracted or {}),
                "piece_shape": list(piece.shape),
            }
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if chrome:
                chrome.terminate()
            xvfb.terminate()
            for proc in [p for p in (chrome, xvfb) if p is not None]:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
            httpd.shutdown()
            httpd.server_close()
            tmp.cleanup()

    def _wait_display(self, display: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["xdpyinfo", "-display", display],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if probe.returncode == 0:
                return
            time.sleep(0.1)
        time.sleep(0.4)


if __name__ == "__main__":
    unittest.main()
