#!/usr/bin/env python3
"""Prosopo image-grid and icon-order matching / CDP solving."""

from __future__ import annotations

import asyncio
import base64
import io
import math
import sys
import unittest
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cookie_server.cdp_browser import CdpBrowser, procaptcha_token  # noqa: E402
from cookie_server.icon_order import (  # noqa: E402
    BOARD_HEIGHT,
    BOARD_WIDTH,
    DEFAULT_TOLERANCE,
    LEGEND_GAP,
    LEGEND_ICON_SIZE,
    match_icon_order,
    split_legend,
)
from cookie_server import cdp_browser as cdp_mod  # noqa: E402
from cookie_server.image_captcha import TileScore, select_tiles  # noqa: E402


def _png_bytes(pixels: np.ndarray) -> bytes:
    image = Image.fromarray(pixels)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _png_data_uri(pixels: np.ndarray) -> str:
    return "data:image/png;base64," + base64.b64encode(_png_bytes(pixels)).decode("ascii")


def _png_b64(pixels: np.ndarray) -> str:
    return base64.b64encode(_png_bytes(pixels)).decode("ascii")


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


def _draw_ring(
    image: np.ndarray,
    cx: float,
    cy: float,
    color: tuple[int, int, int],
    radius: float,
    thickness: float = 2.2,
) -> None:
    height, width = image.shape[:2]
    yy, xx = np.ogrid[0:height, 0:width]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    halo = (dist <= radius + 3.5) & (dist >= max(0.0, radius - thickness - 3.5))
    stroke = (dist <= radius + 0.6) & (dist >= radius - thickness)
    darkened = image[halo].astype(np.float32) * 0.28
    image[halo] = np.clip(darkened, 0, 255).astype(np.uint8)
    image[stroke] = np.array(color, dtype=np.uint8)


def make_synthetic_icon_order(seed: int = 0) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    board = np.zeros((BOARD_HEIGHT, BOARD_WIDTH, 3), dtype=np.uint8)
    board[:] = (208, 198, 184)
    board = np.clip(
        board.astype(np.int16) + rng.integers(-7, 8, board.shape, dtype=np.int16),
        0,
        255,
    ).astype(np.uint8)
    colors = [
        (220, 36, 36),
        (36, 196, 48),
        (36, 72, 220),
        (220, 196, 28),
        (204, 36, 196),
        (28, 196, 196),
        (220, 116, 28),
    ]
    positions = [
        (48.0, 38.0),
        (150.0, 44.0),
        (252.0, 40.0),
        (70.0, 118.0),
        (162.0, 128.0),
        (246.0, 142.0),
        (108.0, 172.0),
    ]
    for (cx, cy), color in zip(positions, colors):
        _draw_ring(board, cx, cy, color, radius=12.0, thickness=2.4)
    target_indices = [0, 2, 4]
    size = LEGEND_ICON_SIZE
    legend = np.zeros((size, size * 3 + LEGEND_GAP * 2, 3), dtype=np.uint8)
    legend[:] = (28, 28, 34)
    for i, index in enumerate(target_indices):
        x0 = i * (size + LEGEND_GAP)
        chip = legend[:, x0 : x0 + size]
        _draw_ring(chip, size / 2, size / 2, colors[index], radius=8.0, thickness=2.0)
    return {
        "board": board,
        "legend": legend,
        "targets": [positions[i] for i in target_indices],
        "frame": {"left": 100.0, "top": 80.0, "width": 300.0, "height": 200.0},
    }


class TileSelectTest(unittest.TestCase):
    def test_selects_tiles_above_the_pack(self) -> None:
        scores = [
            TileScore(0, 0.28, False),
            TileScore(1, 0.16, False),
            TileScore(2, 0.27, False),
            TileScore(3, 0.15, False),
            TileScore(4, 0.31, False),
            TileScore(5, 0.18, False),
            TileScore(6, 0.14, False),
            TileScore(7, 0.26, False),
            TileScore(8, 0.17, False),
        ]
        chosen = {item.index for item in select_tiles(scores) if item.selected}
        self.assertEqual(chosen, {0, 2, 4, 7})

    def test_selects_nothing_when_peak_is_weak(self) -> None:
        scores = [TileScore(i, 0.15 + (i % 3) * 0.01, False) for i in range(9)]
        chosen = [item.index for item in select_tiles(scores) if item.selected]
        self.assertEqual(chosen, [])

    def test_selects_the_peak_when_only_one_is_close(self) -> None:
        scores = [TileScore(i, 0.16, False) for i in range(9)]
        scores[5] = TileScore(5, 0.29, False)
        chosen = [item.index for item in select_tiles(scores) if item.selected]
        self.assertEqual(chosen, [5])


class IconOrderMatchTest(unittest.TestCase):
    def test_split_legend_three_chips(self) -> None:
        scene = make_synthetic_icon_order(1)
        chips = split_legend(scene["legend"])
        self.assertEqual(len(chips), 3)
        self.assertEqual(chips[0].shape[0], LEGEND_ICON_SIZE)
        self.assertEqual(chips[0].shape[1], LEGEND_ICON_SIZE)

    def test_match_recovers_legend_order(self) -> None:
        errors = []
        for seed in range(8):
            scene = make_synthetic_icon_order(seed)
            hits = match_icon_order(scene["board"], scene["legend"])
            self.assertEqual(len(hits), 3, f"seed={seed} hits={hits}")
            for hit, target in zip(hits, scene["targets"]):
                err = math.hypot(hit.x - target[0], hit.y - target[1])
                errors.append(err)
                self.assertLessEqual(
                    err,
                    DEFAULT_TOLERANCE * 38.0,
                    f"seed={seed} target={target} hit=({hit.x:.1f},{hit.y:.1f})",
                )
        self.assertLess(sum(errors) / len(errors), 6.0)


class ImageClearanceTest(unittest.TestCase):
    def test_clearance_clicks_matching_tiles_then_next(self) -> None:
        extract = {
            "ready": True,
            "label": "video projector",
            "roundKey": "video projector|round-1",
            "tiles": [
                {
                    "index": i,
                    "selected": False,
                    "x": 100.0 + (i % 3) * 160.0,
                    "y": 80.0 + (i // 3) * 160.0,
                    "width": 150.0,
                    "height": 150.0,
                    "click": {
                        "x": 175.0 + (i % 3) * 160.0,
                        "y": 155.0 + (i // 3) * 160.0,
                    },
                }
                for i in range(9)
            ],
            "next": {"x": 620.0, "y": 560.0},
        }
        screenshot = np.zeros((720, 800, 3), dtype=np.uint8)
        screenshot[:] = (40, 40, 40)
        for i, tile in enumerate(extract["tiles"]):
            x0 = int(tile["x"])
            y0 = int(tile["y"])
            color = (30 + i * 20, 80, 200 - i * 12)
            screenshot[y0 : y0 + 150, x0 : x0 + 150] = color
        pressed: list[tuple[float, float]] = []
        calls: list[str] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                calls.append(method)
                if method == "Page.captureScreenshot":
                    return {"data": _png_b64(screenshot)}
                if method == "Input.dispatchMouseEvent" and (params or {}).get("type") == "mousePressed":
                    pressed.append((float(params["x"]), float(params["y"])))
                return {}

            async def evaluate(expression: str, session_id: str):
                if "devicePixelRatio" in expression:
                    return 1
                if "captcha image" in expression:
                    return extract
                if pressed:
                    return _snapshot(
                        procaptcha={
                            "present": True,
                            "solved": True,
                            "kind": "solved",
                            "token": "image-token",
                        }
                    )
                return _snapshot(
                    procaptcha={"present": True, "solved": False, "kind": "image"}
                )

            def fake_classify(label, tiles, clip=None):
                self.assertEqual(label, "video projector")
                self.assertEqual(len(tiles), 9)
                return [
                    TileScore(index=i, score=0.30 if i in (0, 4, 8) else 0.12, selected=i in (0, 4, 8))
                    for i in range(len(tiles))
                ]

            original = cdp_mod.classify_tiles
            cdp_mod.classify_tiles = fake_classify  # type: ignore[assignment]
            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            try:
                snapshot, timed_out = await browser._wait_for_page_clearance(
                    "t", "s", timeout=8.0
                )
            finally:
                cdp_mod.classify_tiles = original
            self.assertFalse(timed_out)
            self.assertEqual(procaptcha_token(snapshot), "image-token")

        asyncio.run(run())
        self.assertGreaterEqual(len(pressed), 4)
        self.assertNotIn("Page.reload", calls)
        tile_centers = [
            (extract["tiles"][i]["click"]["x"], extract["tiles"][i]["click"]["y"])
            for i in (0, 4, 8)
        ]
        for expected in tile_centers:
            self.assertTrue(
                any(math.hypot(px - expected[0], py - expected[1]) <= 8.0 for px, py in pressed),
                f"missing click near {expected}, got {pressed}",
            )
        self.assertTrue(
            any(math.hypot(px - 620.0, py - 560.0) <= 6.0 for px, py in pressed),
            pressed,
        )
        markers = [(round(x, 4), round(y, 4)) for x, y in pressed]
        self.assertEqual(len(markers), len(set(markers)))

    def test_same_image_round_is_not_clicked_twice(self) -> None:
        extract = {
            "ready": True,
            "label": "ladder",
            "roundKey": "ladder|same",
            "tiles": [
                {
                    "index": i,
                    "selected": False,
                    "x": 20.0 + i * 40.0,
                    "y": 20.0,
                    "width": 30.0,
                    "height": 30.0,
                    "click": {"x": 35.0 + i * 40.0, "y": 35.0},
                }
                for i in range(9)
            ],
            "next": {"x": 200.0, "y": 80.0},
        }
        screenshot = np.full((120, 400, 3), 80, dtype=np.uint8)
        for i in range(9):
            screenshot[20:50, 20 + i * 40 : 50 + i * 40] = (i * 20, 120, 40)
        pressed: list[tuple[float, float]] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                if method == "Page.captureScreenshot":
                    return {"data": _png_b64(screenshot)}
                if method == "Input.dispatchMouseEvent" and (params or {}).get("type") == "mousePressed":
                    pressed.append((float(params["x"]), float(params["y"])))
                return {}

            async def evaluate(expression: str, session_id: str):
                if "devicePixelRatio" in expression:
                    return 1
                if "captcha image" in expression:
                    return extract
                return _snapshot(
                    procaptcha={"present": True, "solved": False, "kind": "image"}
                )

            def fake_classify(label, tiles, clip=None):
                return [
                    TileScore(index=i, score=0.12, selected=False)
                    for i in range(len(tiles))
                ]

            original = cdp_mod.classify_tiles
            cdp_mod.classify_tiles = fake_classify  # type: ignore[assignment]
            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            try:
                _snapshot_out, timed_out = await browser._wait_for_page_clearance(
                    "t", "s", timeout=4.0
                )
            finally:
                cdp_mod.classify_tiles = original
            self.assertTrue(timed_out)

        asyncio.run(run())
        self.assertEqual(len(pressed), 1)
        self.assertLessEqual(math.hypot(pressed[0][0] - 200.0, pressed[0][1] - 80.0), 6.0)


class IconOrderClearanceTest(unittest.TestCase):
    def test_clearance_clicks_legend_order_then_ok(self) -> None:
        scene = make_synthetic_icon_order(3)
        extract = {
            "ready": True,
            "legendIconSize": LEGEND_ICON_SIZE,
            "frame": scene["frame"],
            "ok": {"x": 430.0, "y": 310.0},
            "legend": _png_data_uri(scene["legend"]),
            "board": _png_data_uri(scene["board"]),
            "roundKey": "icon-round-1",
        }
        pressed: list[tuple[float, float]] = []
        calls: list[str] = []

        async def run() -> None:
            browser = CdpBrowser(None, None, focus_windows=False)

            async def call(method: str, params=None, session_id=None, timeout: float = 30.0):
                calls.append(method)
                if method == "Input.dispatchMouseEvent" and (params or {}).get("type") == "mousePressed":
                    pressed.append((float(params["x"]), float(params["y"])))
                return {}

            async def evaluate(expression: str, session_id: str):
                if "legendIconSize" in expression:
                    return extract
                if len(pressed) >= 4:
                    return _snapshot(
                        procaptcha={
                            "present": True,
                            "solved": True,
                            "kind": "solved",
                            "token": "icon-token",
                        }
                    )
                return _snapshot(
                    procaptcha={"present": True, "solved": False, "kind": "icon_order"}
                )

            browser.call = call  # type: ignore[method-assign]
            browser.evaluate = evaluate  # type: ignore[method-assign]
            snapshot, timed_out = await browser._wait_for_page_clearance(
                "t", "s", timeout=8.0
            )
            self.assertFalse(timed_out)
            self.assertEqual(procaptcha_token(snapshot), "icon-token")

        asyncio.run(run())
        self.assertGreaterEqual(len(pressed), 4)
        self.assertNotIn("Page.reload", calls)
        frame = scene["frame"]
        expected = [
            (
                frame["left"] + (x / BOARD_WIDTH) * frame["width"],
                frame["top"] + (y / BOARD_HEIGHT) * frame["height"],
            )
            for x, y in scene["targets"]
        ]
        icon_clicks = pressed[:3]
        for got, want in zip(icon_clicks, expected):
            self.assertLessEqual(
                math.hypot(got[0] - want[0], got[1] - want[1]),
                DEFAULT_TOLERANCE * 38.0 + 3.0,
                f"got={got} want={want} all={pressed}",
            )
        self.assertLessEqual(math.hypot(pressed[-1][0] - 430.0, pressed[-1][1] - 310.0), 5.0)


class ClipSmokeTest(unittest.TestCase):
    def test_clip_encodes_when_models_are_present(self) -> None:
        from cookie_server.clip_onnx import load_clip, models_ready

        if not models_ready():
            self.skipTest("CLIP models are not on disk")
        clip = load_clip()
        if clip is None:
            self.skipTest("onnxruntime or CLIP failed to load")
        image = Image.fromarray(np.full((64, 64, 3), (20, 180, 40), dtype=np.uint8))
        embeds = clip.encode_images([image])
        texts = clip.encode_texts(["a photo of a lime", "a photo of a submarine"])
        self.assertEqual(embeds.shape[-1], 512)
        self.assertEqual(texts.shape[-1], 512)
        self.assertTrue(np.isfinite(embeds).all())
        scores = embeds @ texts.T
        self.assertTrue(np.isfinite(scores).all())


if __name__ == "__main__":
    unittest.main()
