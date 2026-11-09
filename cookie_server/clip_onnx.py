"""ONNX CLIP (ViT-B/32) for Prosopo image-captcha labels.

Models are the Xenova quantized export of openai/clip-vit-base-patch32.
They already include the 512-d projection heads. Downloaded on first use
(or during the Docker build) into ``cookie_server/models`` or a cache dir.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image

HF_BASE = (
    "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main"
)
VISION_NAME = "vision_model_quantized.onnx"
TEXT_NAME = "text_model_quantized.onnx"
TOKENIZER_NAME = "tokenizer.json"

CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
CLIP_SIZE = 224
SOT = 49406
EOT = 49407
CONTEXT = 77

_BPE_PATTERN = re.compile(
    r"<\|startoftext\|>|<\|endoftext\|>|'s|'t|'re|'ve|'m|'ll|'d|"
    r"[^\W\d_]+|\d|[^\s\w]+",
    re.IGNORECASE | re.UNICODE,
)


def model_dir() -> Path:
    env = os.environ.get("PROSOPO_CLIP_DIR")
    if env:
        return Path(env)
    bundled = Path(__file__).resolve().parent / "models"
    if (bundled / VISION_NAME).is_file():
        return bundled
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "procaptcha-clip"


def models_ready(directory: Path | None = None) -> bool:
    root = directory or model_dir()
    return all(
        (root / name).is_file()
        for name in (VISION_NAME, TEXT_NAME, TOKENIZER_NAME)
    )


def ensure_models(directory: Path | None = None) -> Path:
    root = directory or model_dir()
    root.mkdir(parents=True, exist_ok=True)
    files = {
        VISION_NAME: f"{HF_BASE}/onnx/{VISION_NAME}",
        TEXT_NAME: f"{HF_BASE}/onnx/{TEXT_NAME}",
        TOKENIZER_NAME: f"{HF_BASE}/{TOKENIZER_NAME}",
    }
    for name, url in files.items():
        dest = root / name
        if dest.is_file() and dest.stat().st_size > 1000:
            continue
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        sys.stderr.write(f"[clip_onnx] downloading {name}\n")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
    return root


def _bytes_to_unicode() -> dict[int, str]:
    bs = list(range(ord("!"), ord("~") + 1))
    bs += list(range(ord("¡"), ord("¬") + 1))
    bs += list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for byte in range(256):
        if byte not in bs:
            bs.append(byte)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, (chr(c) for c in cs)))


class ClipTokenizer:
    def __init__(self, tokenizer_path: Path) -> None:
        payload = json.loads(tokenizer_path.read_text())
        model = payload["model"]
        self.vocab: dict[str, int] = dict(model["vocab"])
        self.vocab["<|startoftext|>"] = SOT
        self.vocab["<|endoftext|>"] = EOT
        merges = model["merges"]
        self.ranks: dict[tuple[str, str], int] = {}
        for i, merge in enumerate(merges):
            if isinstance(merge, str):
                left, right = merge.split(" ", 1)
            else:
                left, right = merge
            self.ranks[(left, right)] = i
        self.byte_encoder = _bytes_to_unicode()
        self._cache: dict[str, str] = {
            "<|startoftext|>": "<|startoftext|>",
            "<|endoftext|>": "<|endoftext|>",
        }

    def encode(self, text: str) -> np.ndarray:
        cleaned = re.sub(r"\s+", " ", text.lower(), flags=re.UNICODE).strip()
        tokens = [SOT]
        for piece in _BPE_PATTERN.findall(cleaned):
            encoded = "".join(self.byte_encoder[b] for b in piece.encode("utf-8"))
            tokens.extend(self.vocab[part] for part in self._bpe(encoded).split(" "))
            if len(tokens) >= CONTEXT - 1:
                break
        tokens = tokens[: CONTEXT - 1]
        tokens.append(EOT)
        ids = np.zeros((1, CONTEXT), dtype=np.int64)
        ids[0, : len(tokens)] = tokens
        if len(tokens) < CONTEXT:
            ids[0, len(tokens) :] = EOT
        return ids

    def _bpe(self, token: str) -> str:
        cached = self._cache.get(token)
        if cached is not None:
            return cached
        word: tuple[str, ...] = tuple(token[:-1]) + (token[-1] + "</w>",)
        pairs = _pairs(word)
        if not pairs:
            result = token + "</w>"
            self._cache[token] = result
            return result
        while True:
            bigram = min(pairs, key=lambda p: self.ranks.get(p, 10**9))
            if bigram not in self.ranks:
                break
            first, second = bigram
            new_word: list[str] = []
            i = 0
            while i < len(word):
                try:
                    j = word.index(first, i)
                except ValueError:
                    new_word.extend(word[i:])
                    break
                new_word.extend(word[i:j])
                if j < len(word) - 1 and word[j + 1] == second:
                    new_word.append(first + second)
                    i = j + 2
                else:
                    new_word.append(word[j])
                    i = j + 1
            word = tuple(new_word)
            if len(word) == 1:
                break
            pairs = _pairs(word)
        result = " ".join(word)
        self._cache[token] = result
        return result


def _pairs(word: tuple[str, ...]) -> set[tuple[str, str]]:
    return {(word[i], word[i + 1]) for i in range(len(word) - 1)}


def preprocess_image(image: Image.Image) -> np.ndarray:
    rgb = image.convert("RGB")
    width, height = rgb.size
    if width <= 0 or height <= 0:
        raise ValueError("empty image")
    if width < height:
        new_w = CLIP_SIZE
        new_h = int(round(height * CLIP_SIZE / width))
    else:
        new_h = CLIP_SIZE
        new_w = int(round(width * CLIP_SIZE / height))
    rgb = rgb.resize((new_w, new_h), Image.Resampling.BICUBIC)
    left = max(0, (new_w - CLIP_SIZE) // 2)
    top = max(0, (new_h - CLIP_SIZE) // 2)
    rgb = rgb.crop((left, top, left + CLIP_SIZE, top + CLIP_SIZE))
    if rgb.size != (CLIP_SIZE, CLIP_SIZE):
        rgb = rgb.resize((CLIP_SIZE, CLIP_SIZE), Image.Resampling.BICUBIC)
    arr = np.asarray(rgb, dtype=np.float32) * (1.0 / 255.0)
    arr = (arr - CLIP_MEAN) / CLIP_STD
    return np.transpose(arr, (2, 0, 1))[None, ...]


class ClipOnnx:
    def __init__(self, directory: Path | None = None) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError("onnxruntime is required for image captcha") from exc
        root = ensure_models(directory)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(os.environ.get("PROSOPO_CLIP_THREADS", "2"))
        self._vision = ort.InferenceSession(
            str(root / VISION_NAME), opts, providers=["CPUExecutionProvider"]
        )
        self._text = ort.InferenceSession(
            str(root / TEXT_NAME), opts, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = ClipTokenizer(root / TOKENIZER_NAME)

    def encode_images(self, images: Iterable[Image.Image]) -> np.ndarray:
        batch = np.concatenate([preprocess_image(image) for image in images], axis=0)
        embeds = self._vision.run(None, {"pixel_values": batch})[0]
        return _l2norm(np.asarray(embeds, dtype=np.float32))

    def encode_texts(self, texts: Iterable[str]) -> np.ndarray:
        ids = np.concatenate([self._tokenizer.encode(text) for text in texts], axis=0)
        embeds = self._text.run(None, {"input_ids": ids})[0]
        return _l2norm(np.asarray(embeds, dtype=np.float32))


def _l2norm(arr: np.ndarray) -> np.ndarray:
    denom = np.linalg.norm(arr, axis=-1, keepdims=True)
    return arr / np.maximum(denom, 1e-8)


@lru_cache(maxsize=1)
def load_clip() -> ClipOnnx | None:
    if os.environ.get("PROSOPO_CLIP") == "0":
        return None
    try:
        return ClipOnnx()
    except Exception as exc:
        sys.stderr.write(f"[clip_onnx] unavailable: {exc}\n")
        return None
