"""Classify Prosopo 3x3 image-captcha tiles against the prompt label.

Tiles are Caltech-style object photos. CLIP ViT-B/32 scores each tile against
prompt templates for the target; tiles clearly above the pack are selected.
The provider grades each round at ``imageThreshold`` (default 0.8) over nine
cells, so one miss is tolerated and two are not.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from PIL import Image

from cookie_server.puzzle import decode_image

PROMPT_TEMPLATES = (
    "a photo of a {label}",
    "a photo of {label}",
    "{label}",
    "a close-up photo of a {label}",
)


@dataclass(frozen=True)
class TileScore:
    index: int
    score: float
    selected: bool


def _as_pil(image: Image.Image | np.ndarray | str | bytes) -> Image.Image:
    if isinstance(image, Image.Image):
        return image.convert("RGB")
    if isinstance(image, np.ndarray):
        arr = image
        if arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype(np.uint8)
        if arr.ndim == 2:
            return Image.fromarray(arr, mode="L").convert("RGB")
        if arr.shape[-1] == 4:
            return Image.fromarray(arr, mode="RGBA").convert("RGB")
        return Image.fromarray(arr[..., :3], mode="RGB")
    pixels = decode_image(image)
    return _as_pil(pixels)


def score_tiles(
    label: str,
    tiles: Sequence[Image.Image | np.ndarray | str | bytes],
    *,
    clip=None,
) -> list[TileScore]:
    """Return a CLIP similarity for each tile against ``label``."""
    if clip is None:
        from cookie_server.clip_onnx import load_clip

        clip = load_clip()
    if clip is None:
        raise RuntimeError("CLIP model is not available")
    label = " ".join(str(label).strip().split())
    if not label:
        raise ValueError("empty image-captcha label")
    images = [_as_pil(tile) for tile in tiles]
    if not images:
        return []
    image_emb = clip.encode_images(images)
    texts = [template.format(label=label) for template in PROMPT_TEMPLATES]
    text_emb = clip.encode_texts(texts)
    text_vec = _l2norm(text_emb.mean(axis=0, keepdims=True))[0]
    scores = image_emb @ text_vec
    return [
        TileScore(index=i, score=float(score), selected=False)
        for i, score in enumerate(scores)
    ]


def select_tiles(scores: Sequence[TileScore]) -> list[TileScore]:
    """Pick tiles that match the prompt, or none if nothing is close."""
    if not scores:
        return []
    values = np.array([item.score for item in scores], dtype=np.float32)
    peak = float(values.max())
    mean = float(values.mean())
    std = float(values.std())
    # Matching Caltech photos sit around 0.25–0.32; decoys around 0.14–0.22.
    # Require both a peak-relative cut and a pack cut so a tight cluster of
    # middling scores is not mass-selected.
    abs_cut = 0.235
    if peak < 0.20:
        chosen = np.zeros(len(scores), dtype=bool)
    else:
        rel_cut = max(abs_cut, peak - 0.05)
        pack_cut = mean + max(0.035, 0.7 * std)
        chosen = (values >= rel_cut) & (values >= pack_cut)
        if not bool(chosen.any()) and peak >= abs_cut:
            chosen[int(values.argmax())] = True
    return [
        TileScore(index=item.index, score=item.score, selected=bool(chosen[i]))
        for i, item in enumerate(scores)
    ]


def classify_tiles(
    label: str,
    tiles: Sequence[Image.Image | np.ndarray | str | bytes],
    *,
    clip=None,
) -> list[TileScore]:
    return select_tiles(score_tiles(label, tiles, clip=clip))


def _l2norm(arr: np.ndarray) -> np.ndarray:
    denom = np.linalg.norm(arr, axis=-1, keepdims=True)
    return arr / np.maximum(denom, 1e-8)


def png_from_array(pixels: np.ndarray) -> bytes:
    image = Image.fromarray(pixels)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
