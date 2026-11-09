#!/usr/bin/env python3
"""Prosopo puzzle matching and CDP drag."""

from __future__ import annotations

import asyncio
import base64
import io
import math
import random
import sys
import unittest
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cookie_server.cdp_browser import CdpBrowser, procaptcha_token  # noqa: E402
from cookie_server.puzzle import (  # noqa: E402
    BOARD_HEIGHT,
    BOARD_WIDTH,
    decode_image,
    human_drag_path,
    match_piece,
)


def _png_data_uri(pixels: np.ndarray) -> str:
    image = Image.fromarray(pixels)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


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

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                calls.append(method)
                if method == "Input.dispatchMouseEvent" and (params or {}).get("type") == "mouseReleased":
                    released["n"] += 1
                return {}

            async def evaluate(expression: str, session_id: str):
                if "toDataURL" in expression:
                    return extract
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

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            snapshot, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=8.0
            )
            self.assertFalse(timed_out)
            self.assertEqual(procaptcha_token(snapshot), "puzzle-token")

        asyncio.run(run())
        self.assertGreaterEqual(released["n"], 1)
        self.assertGreater(calls.count("Input.dispatchMouseEvent"), 4)
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


if __name__ == "__main__":
    unittest.main()
