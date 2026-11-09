"""Chrome DevTools Protocol automation: open tab, load URL, collect cookies, close."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import random
import sys
import urllib.request
from typing import Any, Callable, Sequence
from urllib.parse import urlparse, urlunparse

from cookie_server.ws import ws_close, ws_connect, ws_mask, ws_recv

try:
    from cookie_server.puzzle import (
        board_rect_from_state,
        decode_image,
        human_drag_path,
        match_piece,
    )

    _PUZZLE_MATCH_AVAILABLE = True
except ImportError:  # numpy / Pillow missing from the image
    board_rect_from_state = None  # type: ignore[assignment]
    decode_image = None  # type: ignore[assignment]
    human_drag_path = None  # type: ignore[assignment]
    match_piece = None  # type: ignore[assignment]
    _PUZZLE_MATCH_AVAILABLE = False

try:
    from cookie_server.icon_order import (
        BOARD_HEIGHT as ICON_BOARD_HEIGHT,
        BOARD_WIDTH as ICON_BOARD_WIDTH,
        decode_icon_images,
        match_icon_order,
    )

    _ICON_ORDER_AVAILABLE = True
except ImportError:
    ICON_BOARD_HEIGHT = 200  # type: ignore[assignment]
    ICON_BOARD_WIDTH = 300  # type: ignore[assignment]
    decode_icon_images = None  # type: ignore[assignment]
    match_icon_order = None  # type: ignore[assignment]
    _ICON_ORDER_AVAILABLE = False

try:
    from cookie_server.image_captcha import classify_tiles

    _IMAGE_CAPTCHA_AVAILABLE = True
except ImportError:
    classify_tiles = None  # type: ignore[assignment]
    _IMAGE_CAPTCHA_AVAILABLE = False

CF_MARKERS = (
    "one moment please",
    "just a moment",
    "attention required",
    "checking your browser",
    "enable javascript and cookies",
)

CF_DOM_MARKERS = (
    "challenge-platform",
    "cf-mitigated",
    "cf-browser-verification",
    "cf-turnstile",
)

CF_INTERSTITIAL_BODY_LEN = 5371
CF_POST_CLICK_BODY_LEN = 5985

# Procaptcha inspects light DOM and open shadow roots. Checkbox / invisible
# PoW is clicked; the puzzle overlay is matched and dragged; image grids are
# classified with CLIP; icon-order legend chips are matched onto the board.
# Unknown dialogs stay kind=visual and are left unsolved.
_PROSOPO_INSPECT_JS = """
function inspectProcaptcha() {
  try {
    var tokenEl = document.querySelector('[name="procaptcha-response"]');
    var token = tokenEl && tokenEl.value ? String(tokenEl.value).trim() : '';
    var widget = document.querySelector(
      '.procaptcha, .p-procaptcha, .prosopo-widget, .prosopo-checkbox, prosopo-procaptcha, .prosopo-puzzle-piece'
    );
    if (!widget && !token) {
      return { present: false, solved: false };
    }
    if (token) {
      return { present: true, solved: true, kind: 'solved', token: token };
    }

    function clickPoint(el, xRatio) {
      try {
        el.scrollIntoView({ block: 'center', inline: 'center' });
      } catch (e) {}
      var r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) return null;
      var ratio = xRatio == null ? 0.5 : xRatio;
      return {
        x: Math.round(r.left + Math.min(Math.max(8, r.width * ratio), r.width - 2)),
        y: Math.round(r.top + r.height / 2),
        width: Math.round(r.width),
        height: Math.round(r.height)
      };
    }

    function walkShadows(root, visit) {
      if (!root) return null;
      var hit = visit(root);
      if (hit) return hit;
      if (root.shadowRoot) {
        var fromRoot = walkShadows(root.shadowRoot, visit);
        if (fromRoot) return fromRoot;
      }
      var nodes = root.querySelectorAll('*');
      for (var n = 0; n < nodes.length; n++) {
        if (nodes[n].shadowRoot) {
          var nested = walkShadows(nodes[n].shadowRoot, visit);
          if (nested) return nested;
        }
      }
      return null;
    }

    function findCheckbox(root) {
      return walkShadows(root, function (scope) {
        var inputs = scope.querySelectorAll('input[type="checkbox"]');
        for (var i = 0; i < inputs.length; i++) {
          var input = inputs[i];
          if (input.disabled) continue;
          var ir = input.getBoundingClientRect();
          if (ir.width >= 8 && ir.height >= 8) return input;
        }
        return null;
      });
    }

    function findSpinner(root) {
      return walkShadows(root, function (scope) {
        return scope.querySelector('[role="progressbar"], [role="status"]');
      });
    }

    function findPuzzlePiece(root) {
      // Live widget paints a hidden, aria-hidden clone first. That node has
      // no background and is not the draggable overlay.
      return walkShadows(root, function (scope) {
        var nodes = scope.querySelectorAll('.prosopo-puzzle-piece');
        var fallback = null;
        for (var p = 0; p < nodes.length; p++) {
          var el = nodes[p];
          var cs = window.getComputedStyle(el);
          if (el.getAttribute('aria-hidden') === 'true') continue;
          if (cs.opacity === '0' || cs.pointerEvents === 'none') continue;
          var bg = cs.backgroundImage || '';
          if (bg && bg !== 'none') return el;
          if (!fallback) fallback = el;
        }
        return fallback;
      });
    }

    var puzzlePiece = findPuzzlePiece(document);
    if (puzzlePiece) {
      var area = puzzlePiece.parentElement;
      var ar = area ? area.getBoundingClientRect() : puzzlePiece.getBoundingClientRect();
      var pr = puzzlePiece.getBoundingClientRect();
      if (ar.width >= 40 && ar.height >= 40 && pr.width >= 8 && pr.height >= 8) {
        var pieceSize = puzzlePiece.offsetWidth || pr.width;
        return {
          present: true,
          solved: false,
          kind: 'puzzle',
          originX: (puzzlePiece.offsetLeft || 0) + pieceSize / 2,
          originY: (puzzlePiece.offsetTop || 0) + (puzzlePiece.offsetHeight || pr.height) / 2,
          pieceSize: pieceSize,
          board: {
            left: ar.left,
            top: ar.top,
            width: ar.width,
            height: ar.height
          },
          piece: {
            left: pr.left,
            top: pr.top,
            width: pr.width,
            height: pr.height
          }
        };
      }
    }

    var dialogs = document.querySelectorAll('[role="dialog"], [role="alertdialog"]');
    for (var d = 0; d < dialogs.length; d++) {
      var dr = dialogs[d].getBoundingClientRect();
      if (dr.width >= 80 && dr.height >= 80) {
        var dialogText = '';
        var dialogAria = '';
        try { dialogText = String(dialogs[d].textContent || ''); } catch (e) {}
        try { dialogAria = String(dialogs[d].getAttribute('aria-label') || ''); } catch (e) {}
        var blob = dialogAria + ' ' + dialogText;
        if (/icon order challenge|select in this order|icons to select/i.test(blob)) {
          return { present: true, solved: false, kind: 'icon_order' };
        }
        if (/select all containing|image challenge/i.test(blob)) {
          return { present: true, solved: false, kind: 'image' };
        }
        // The puzzle overlay is itself a dialog. Before the piece is
        // measurable, treat it as still loading rather than as an
        // unknown visual captcha. Icon-order retry also says "Not quite"
        // and is classified above.
        if (/drag the piece|puzzle challenge|not quite/i.test(blob)) {
          return { present: true, solved: false, kind: 'pending', loading: true };
        }
        return { present: true, solved: false, kind: 'visual' };
      }
    }

    var checkbox = null;
    var hosts = document.querySelectorAll('.prosopo-checkbox');
    for (var h = 0; h < hosts.length; h++) {
      checkbox = findCheckbox(hosts[h].shadowRoot || hosts[h]);
      if (checkbox) break;
    }
    if (!checkbox) checkbox = findCheckbox(document);

    if (checkbox) {
      var boxClick = clickPoint(checkbox, 0.5);
      if (!boxClick) {
        return { present: true, solved: false, kind: 'pending', loading: true };
      }
      return {
        present: true,
        solved: false,
        kind: 'checkbox',
        click: boxClick
      };
    }

    if (widget && findSpinner(widget)) {
      return { present: true, solved: false, kind: 'pending', loading: true };
    }

    var invisible = document.querySelector(
      '.procaptcha[data-size="invisible"], button.procaptcha'
    );
    if (invisible && window.procaptcha && typeof window.procaptcha.execute === 'function') {
      return { present: true, solved: false, kind: 'invisible' };
    }

    var clickHost = document.querySelector(
      '.prosopo-checkbox, .prosopo-widget__content, .prosopo-widget, .procaptcha, .p-procaptcha'
    );
    if (clickHost) {
      var hostClick = clickPoint(clickHost, 0.12);
      if (hostClick) {
        return {
          present: true,
          solved: false,
          kind: 'checkbox',
          click: hostClick
        };
      }
    }

    return { present: true, solved: false, kind: 'pending' };
  } catch (err) {
    return { present: false, solved: false, error: String(err) };
  }
}
"""

_PAGE_SNAPSHOT_JS = (
    "(() => {\n"
    + _PROSOPO_INSPECT_JS
    + """
  return {
    ready: document.readyState || '',
    title: document.title || '',
    href: location.href || '',
    html: document.documentElement ? document.documentElement.outerHTML : '',
    procaptcha: inspectProcaptcha()
  };
})()"""
)

_PROSOPO_EXECUTE_JS = """(() => {
  try {
    if (window.procaptcha && typeof window.procaptcha.execute === 'function') {
      window.procaptcha.execute();
      return { executed: true, via: 'api' };
    }
  } catch (e) {}
  document.dispatchEvent(new CustomEvent('procaptcha:execute', {
    bubbles: true,
    cancelable: true
  }));
  return { executed: true, via: 'event' };
})()"""

_PROSOPO_PUZZLE_EXTRACT_JS = """(async () => {
  function walkShadows(root, visit) {
    if (!root) return null;
    var hit = visit(root);
    if (hit) return hit;
    if (root.shadowRoot) {
      var fromRoot = walkShadows(root.shadowRoot, visit);
      if (fromRoot) return fromRoot;
    }
    var nodes = root.querySelectorAll('*');
    for (var n = 0; n < nodes.length; n++) {
      if (nodes[n].shadowRoot) {
        var nested = walkShadows(nodes[n].shadowRoot, visit);
        if (nested) return nested;
      }
    }
    return null;
  }
  function cssUrl(value) {
    if (!value || value === 'none') return '';
    var match = String(value).match(/url\\((['\"]?)(.+?)\\1\\)/);
    if (!match) return '';
    return String(match[2] || '').replace(/^['\"]|['\"]$/g, '');
  }
  async function rasterize(src, width, height) {
    if (!src) return '';
    var img = new Image();
    img.src = src;
    await img.decode();
    var w = width || img.naturalWidth || img.width;
    var h = height || img.naturalHeight || img.height;
    if (!w || !h) return '';
    var canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, w, h);
    ctx.drawImage(img, 0, 0, w, h);
    return canvas.toDataURL('image/png');
  }
  var piece = walkShadows(document, function (scope) {
    var nodes = scope.querySelectorAll('.prosopo-puzzle-piece');
    var fallback = null;
    for (var p = 0; p < nodes.length; p++) {
      var el = nodes[p];
      var cs = getComputedStyle(el);
      if (el.getAttribute('aria-hidden') === 'true') continue;
      if (cs.opacity === '0' || cs.pointerEvents === 'none') continue;
      var bg = cs.backgroundImage || '';
      if (bg && bg !== 'none') return el;
      if (!fallback) fallback = el;
    }
    return fallback;
  });
  if (!piece) return { ready: false, reason: 'no_piece' };
  var area = piece.parentElement;
  if (!area) return { ready: false, reason: 'no_board' };
  var boardRect = area.getBoundingClientRect();
  var pieceRect = piece.getBoundingClientRect();
  if (boardRect.width < 40 || boardRect.height < 40) {
    return { ready: false, reason: 'board_size' };
  }
  var backgroundSrc = '';
  var tiles = area.querySelectorAll('img');
  for (var i = 0; i < tiles.length; i++) {
    var tile = tiles[i];
    if (tile && tile.src) {
      // Centre kaleidoscope tile is at local (300, 200); any tile's src
      // is the same data URI, so the first non-empty src is enough.
      backgroundSrc = tile.src;
      if (Math.abs((parseFloat(tile.style.left) || 0) - 300) < 1 &&
          Math.abs((parseFloat(tile.style.top) || 0) - 200) < 1) {
        break;
      }
    }
  }
  if (!backgroundSrc) {
    backgroundSrc = cssUrl(getComputedStyle(area).backgroundImage);
  }
  var pieceSrc = cssUrl(getComputedStyle(piece).backgroundImage);
  if (!backgroundSrc || !pieceSrc) {
    return { ready: false, reason: 'missing_src' };
  }
  var pieceSize = piece.offsetWidth || pieceRect.width;
  var background;
  var overlay;
  try {
    background = await rasterize(backgroundSrc, 300, 200);
    overlay = await rasterize(pieceSrc, Math.round(pieceSize), Math.round(piece.offsetHeight || pieceSize));
  } catch (err) {
    return { ready: false, reason: 'decode', error: String(err) };
  }
  if (!background || !overlay) {
    return { ready: false, reason: 'empty_raster' };
  }
  return {
    ready: true,
    originX: (piece.offsetLeft || 0) + pieceSize / 2,
    originY: (piece.offsetTop || 0) + (piece.offsetHeight || pieceRect.height) / 2,
    pieceSize: pieceSize,
    board: {
      left: boardRect.left,
      top: boardRect.top,
      width: boardRect.width,
      height: boardRect.height
    },
    pieceRect: {
      left: pieceRect.left,
      top: pieceRect.top,
      width: pieceRect.width,
      height: pieceRect.height
    },
    background: background,
    piece: overlay
  };
})()"""

_PROSOPO_IMAGE_EXTRACT_JS = """(async () => {
  function clickPoint(el) {
    if (!el) return null;
    var r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return null;
    return {
      x: r.left + r.width / 2,
      y: r.top + r.height / 2,
      left: r.left,
      top: r.top,
      width: r.width,
      height: r.height
    };
  }
  function clickableOf(node) {
    var el = node;
    while (el && el !== document.body) {
      var role = '';
      try { role = String(el.getAttribute('role') || ''); } catch (e) {}
      if (el.tagName === 'BUTTON' || role === 'button') return el;
      el = el.parentElement;
    }
    return node;
  }
  var dialogs = document.querySelectorAll('[role="dialog"], [role="alertdialog"]');
  var dialog = null;
  for (var d = 0; d < dialogs.length; d++) {
    var dr = dialogs[d].getBoundingClientRect();
    if (dr.width >= 80 && dr.height >= 80) { dialog = dialogs[d]; break; }
  }
  if (!dialog) return { ready: false, reason: 'no_dialog' };
  var dialogText = String(dialog.textContent || '');
  var dialogAria = String(dialog.getAttribute('aria-label') || '');
  var blob = dialogAria + ' ' + dialogText;
  var label = '';
  var match = blob.match(/Select all containing(?: the following)?:\\s*([\\s\\S]+?)\\s*If there are none/i);
  if (match) label = String(match[1] || '').replace(/\\u00a0/g, ' ').trim();
  var imgs = dialog.querySelectorAll('img');
  var tiles = [];
  var pending = 0;
  for (var i = 0; i < imgs.length; i++) {
    var img = imgs[i];
    var alt = String(img.alt || '');
    if (!/captcha image/i.test(alt)) continue;
    try { await img.decode(); } catch (e) { pending++; }
    var clickable = clickableOf(img);
    var rect = img.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 20) continue;
    if (!img.naturalWidth || img.naturalWidth < 8) {
      pending++;
      continue;
    }
    tiles.push({
      index: tiles.length,
      alt: alt,
      src: String(img.currentSrc || img.src || '').slice(0, 240),
      selected: String(clickable.getAttribute('aria-pressed') || '') === 'true',
      x: rect.left,
      y: rect.top,
      width: rect.width,
      height: rect.height,
      click: clickPoint(clickable)
    });
  }
  var next = null;
  var controls = dialog.querySelectorAll('button, [role="button"]');
  for (var c = 0; c < controls.length; c++) {
    var el = controls[c];
    var t = (String(el.getAttribute('aria-label') || '') + ' ' + String(el.textContent || '')).replace(/\\s+/g, ' ').trim();
    var aria = String(el.getAttribute('aria-label') || '');
    if (/^(next|submit)$/i.test(t) || /^(next|submit)\\b/i.test(aria) || /^(next|submit)$/i.test(String(el.textContent || '').trim())) {
      next = clickPoint(el);
      if (next) next.text = t;
    }
  }
  if (pending || tiles.length < 9 || !label) {
    return {
      ready: false,
      reason: 'incomplete',
      label: label,
      tileCount: tiles.length,
      pending: pending
    };
  }
  return {
    ready: true,
    kind: 'image',
    label: label,
    roundKey: label + '|' + tiles.map(function (t) { return t.src || (t.x + ',' + t.y); }).join('|'),
    tiles: tiles,
    next: next
  };
})()"""

_PROSOPO_ICON_EXTRACT_JS = """(async () => {
  async function rasterize(img, width, height) {
    if (!img) return '';
    try { await img.decode(); } catch (e) {}
    var w = width || img.naturalWidth || Math.round(img.width);
    var h = height || img.naturalHeight || Math.round(img.height);
    if (!w || !h) return '';
    var canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    var ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0, w, h);
    return canvas.toDataURL('image/png');
  }
  function clickPoint(el) {
    if (!el) return null;
    var r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return null;
    return {
      x: r.left + r.width / 2,
      y: r.top + r.height / 2,
      left: r.left,
      top: r.top,
      width: r.width,
      height: r.height
    };
  }
  var dialogs = document.querySelectorAll('[role="dialog"], [role="alertdialog"]');
  var dialog = null;
  for (var d = 0; d < dialogs.length; d++) {
    var dr = dialogs[d].getBoundingClientRect();
    if (dr.width >= 80 && dr.height >= 80) { dialog = dialogs[d]; break; }
  }
  if (!dialog) return { ready: false, reason: 'no_dialog' };
  var imgs = Array.from(dialog.querySelectorAll('img'));
  var legendImg = null;
  var boardImg = null;
  for (var i = 0; i < imgs.length; i++) {
    var img = imgs[i];
    var alt = String(img.alt || '');
    var nw = img.naturalWidth || 0;
    var nh = img.naturalHeight || 0;
    if (/icons to select/i.test(alt) || (nh && nh <= 40 && nw >= 40)) {
      legendImg = img;
    } else if (nw >= 200 && nh >= 120) {
      boardImg = img;
    }
  }
  if (!boardImg) {
    for (var j = 0; j < imgs.length; j++) {
      var r = imgs[j].getBoundingClientRect();
      if (r.width >= 200 && r.height >= 120) boardImg = imgs[j];
    }
  }
  if (!legendImg || !boardImg) {
    return { ready: false, reason: 'missing_images', imgs: imgs.length };
  }
  var frame = boardImg.getBoundingClientRect();
  if (frame.width < 200 || frame.height < 120) {
    return { ready: false, reason: 'frame_size' };
  }
  var ok = null;
  var controls = dialog.querySelectorAll('button, [role="button"]');
  for (var c = 0; c < controls.length; c++) {
    var t = (String(controls[c].getAttribute('aria-label') || '') + ' ' + String(controls[c].textContent || '')).trim();
    if (/^(ok|okay|checking)/i.test(t)) ok = clickPoint(controls[c]);
  }
  var legendPng;
  var boardPng;
  try {
    legendPng = await rasterize(legendImg);
    boardPng = await rasterize(boardImg, 300, 200);
  } catch (err) {
    return { ready: false, reason: 'decode', error: String(err) };
  }
  if (!legendPng || !boardPng) return { ready: false, reason: 'empty_raster' };
  return {
    ready: true,
    kind: 'icon_order',
    legendIconSize: legendImg.naturalHeight || legendImg.height || 26,
    frame: { left: frame.left, top: frame.top, width: frame.width, height: frame.height },
    ok: ok,
    legend: legendPng,
    board: boardPng,
    roundKey: String(legendImg.src || '').slice(0, 80) + '|' + String(boardImg.src || '').slice(0, 80)
  };
})()"""

_TURNSTILE_IFRAME_JS = """(() => {
  const selectors = [
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="turnstile"]',
    'iframe[id^="cf-chl-widget"]',
    '#challenge-stage iframe',
    '.cf-turnstile iframe',
  ];
  let iframe = null;
  for (const sel of selectors) {
    iframe = document.querySelector(sel);
    if (iframe) break;
  }
  if (!iframe) {
    const widget = document.querySelector('.cf-turnstile');
    if (widget) {
      const rect = widget.getBoundingClientRect();
      if (rect.width >= 1 && rect.height >= 1) {
        return {
          x: Math.round(rect.left + Math.min(28, rect.width * 0.15)),
          y: Math.round(rect.top + rect.height / 2),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        };
      }
    }
    return null;
  }
  iframe.scrollIntoView({ block: 'center', inline: 'center' });
  const rect = iframe.getBoundingClientRect();
  if (rect.width < 1 || rect.height < 1) return null;
  return {
    x: Math.round(rect.left + Math.min(28, rect.width * 0.15)),
    y: Math.round(rect.top + rect.height / 2),
    width: Math.round(rect.width),
    height: Math.round(rect.height),
  };
})()"""

_SUBMIT_TURNSTILE_JS = """(() => {
  const tokenInput = document.querySelector('[name="cf-turnstile-response"]');
  const token = tokenInput && tokenInput.value ? String(tokenInput.value).trim() : '';
  if (!token) {
    return { submitted: false, reason: 'no_token' };
  }
  const btn = document.getElementById('submit-btn');
  if (btn) {
    btn.disabled = false;
    btn.click();
    return { submitted: true, via: 'button' };
  }
  const form = document.getElementById('captcha-form');
  if (form) {
    if (typeof form.requestSubmit === 'function') {
      form.requestSubmit();
    } else {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    }
    return { submitted: true, via: 'form' };
  }
  return { submitted: false, reason: 'no_form' };
})()"""


def _challenge_blob(title: str, href: str, html: str) -> str:
    return f"{title}\n{href}\n{html}".lower()


def is_challenge_page(
    title: str,
    href: str,
    html: str,
    *,
    body_len: int | None = None,
) -> bool:
    blob = _challenge_blob(title, href, html)
    if any(marker in blob for marker in CF_MARKERS):
        return True
    if any(marker in blob for marker in CF_DOM_MARKERS):
        return True
    html_len = len(html.encode("utf-8"))
    if body_len == CF_INTERSTITIAL_BODY_LEN or html_len == CF_INTERSTITIAL_BODY_LEN:
        return True
    if (
        body_len is not None
        and abs(body_len - CF_POST_CLICK_BODY_LEN) <= 80
    ) or abs(html_len - CF_POST_CLICK_BODY_LEN) <= 80:
        return True
    return False


def is_interstitial_body(content: bytes) -> bool:
    if len(content) == CF_INTERSTITIAL_BODY_LEN:
        return True
    text = content.decode("utf-8", errors="replace")
    return any(marker in text.lower() for marker in CF_DOM_MARKERS + CF_MARKERS)


def procaptcha_state(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    state = (snapshot or {}).get("procaptcha")
    return state if isinstance(state, dict) else {}


def pending_procaptcha(snapshot: dict[str, Any] | None) -> bool:
    state = procaptcha_state(snapshot)
    return bool(state.get("present") and not state.get("solved"))


def procaptcha_token(snapshot: dict[str, Any] | None) -> str:
    state = procaptcha_state(snapshot)
    token = state.get("token")
    return str(token).strip() if token else ""

BROWSER_HEADER_KEYS = (
    "User-Agent",
    "Accept-Language",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
    "sec-ch-ua-full-version-list",
)

_CAPTURE_HEADERS_JS = """(async () => {
  const out = {};
  out["User-Agent"] = navigator.userAgent;
  const langs = navigator.languages && navigator.languages.length
    ? navigator.languages
    : [navigator.language || "en-US"];
  out["Accept-Language"] = langs
    .map((lang, i) => (i === 0 ? lang : `${lang};q=${(1 - i * 0.1).toFixed(1)}`))
    .join(", ");
  const data = navigator.userAgentData;
  if (!data) {
    return out;
  }
  out["sec-ch-ua-mobile"] = data.mobile ? "?1" : "?0";
  out["sec-ch-ua-platform"] = `"${data.platform}"`;
  out["sec-ch-ua"] = data.brands
    .map((b) => `"${b.brand}";v="${b.version}"`)
    .join(", ");
  try {
    const hints = await data.getHighEntropyValues(["platformVersion", "fullVersionList"]);
    out["sec-ch-ua-platform-version"] = `"${hints.platformVersion}"`;
    out["sec-ch-ua-full-version-list"] = hints.fullVersionList
      .map((b) => `"${b.brand}";v="${b.version}"`)
      .join(", ");
  } catch (e) {}
  return out;
})()"""


def pick_browser_headers(raw: dict[str, str]) -> dict[str, str]:
    lower = {key.lower(): value for key, value in raw.items()}
    picked: dict[str, str] = {}
    for key in BROWSER_HEADER_KEYS:
        value = raw.get(key) or lower.get(key.lower())
        if value:
            picked[key] = str(value)
    return picked


def get_browser_user_agent(cdp_http: str) -> str:
    url = cdp_http.rstrip("/") + "/json/version"
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read().decode())
    ua = data.get("User-Agent")
    if not ua:
        raise RuntimeError(f"no User-Agent in CDP version at {cdp_http}")
    return str(ua)


def discover_browser_ws(cdp_http: str) -> str:
    url = cdp_http.rstrip("/") + "/json/version"
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read().decode())
    ws_url = data.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError(f"no browser WebSocket at {cdp_http}")
    parsed_ws = urlparse(ws_url)
    parsed_http = urlparse(cdp_http)
    return urlunparse(
        (
            "ws",
            parsed_http.netloc,
            parsed_ws.path,
            parsed_ws.params,
            parsed_ws.query,
            parsed_ws.fragment,
        )
    )


class CdpBrowser:
    def __init__(self, reader, writer, *, focus_windows: bool = True) -> None:
        self.reader = reader
        self.writer = writer
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._event_waiters: list[tuple[str, str | None, asyncio.Future]] = []
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self._work_target_id: str | None = None
        self._work_session_id: str | None = None
        # True only for a single-slot pool. Parallel slots must not steal
        # OS focus from each other; Turnstile is clicked via CDP input.
        self._focus_windows = focus_windows

    @classmethod
    async def connect(
        cls,
        cdp_http: str,
        *,
        focus_windows: bool = True,
    ) -> "CdpBrowser":
        ws_url = discover_browser_ws(cdp_http)
        reader, writer = await ws_connect(ws_url)
        browser = cls(reader, writer, focus_windows=focus_windows)
        browser._recv_task = asyncio.create_task(browser._recv_loop())
        return browser

    async def close(self) -> None:
        self._closed = True
        self._work_target_id = None
        self._work_session_id = None
        self._fail_pending(ConnectionError("CDP connection closed"))
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        await ws_close(self.writer)

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        for _, _, fut in self._event_waiters:
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        self._event_waiters.clear()

    def _dispatch_event(self, method: str, session_id: str | None, params: dict) -> None:
        remaining: list[tuple[str, str | None, asyncio.Future]] = []
        for waited_method, waited_session, fut in self._event_waiters:
            if fut.done():
                continue
            if waited_method != method:
                remaining.append((waited_method, waited_session, fut))
                continue
            if waited_session is not None and waited_session != session_id:
                remaining.append((waited_method, waited_session, fut))
                continue
            fut.set_result(params)
        self._event_waiters = remaining

    async def _recv_loop(self) -> None:
        try:
            while not self._closed:
                raw = await ws_recv(self.reader)
                data = json.loads(raw)
                msg_id = data.get("id")
                if msg_id is not None:
                    fut = self._pending.pop(msg_id, None)
                    if fut and not fut.done():
                        fut.set_result(data)
                    continue
                method = data.get("method")
                if method:
                    self._dispatch_event(method, data.get("sessionId"), data.get("params") or {})
        except asyncio.CancelledError:
            self._fail_pending(ConnectionError("CDP connection closed"))
            raise
        except Exception:
            self._fail_pending(ConnectionError("CDP connection closed"))
        finally:
            self._closed = True

    async def call(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> Any:
        self._next_id += 1
        msg_id = self._next_id
        payload: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        self.writer.write(ws_mask(json.dumps(payload).encode(), opcode=0x1))
        await self.writer.drain()
        try:
            data = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise TimeoutError(f"CDP {method} timed out after {timeout}s")
        if "error" in data:
            raise RuntimeError(f"CDP {method}: {data['error']}")
        return data.get("result")

    async def wait_for_event(
        self,
        method: str,
        session_id: str | None = None,
        timeout: float = 60.0,
    ) -> dict:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._event_waiters.append((method, session_id, fut))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._event_waiters = [
                item for item in self._event_waiters if item[2] is not fut
            ]
            raise TimeoutError(f"timed out waiting for {method}")

    async def capture_document_headers(
        self,
        url: str,
        timeout: float = 15.0,
    ) -> dict[str, str]:
        target_id: str | None = None
        session_id: str | None = None
        try:
            created = await self.call("Target.createTarget", {"url": "about:blank"})
            target_id = created["targetId"]
            attached = await self.call(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
            session_id = attached["sessionId"]

            await self.call("Page.enable", session_id=session_id)
            await self.call("Network.enable", session_id=session_id)

            captured: dict[str, str] = {}
            headers_found = asyncio.get_event_loop().create_future()

            async def collect_document_headers() -> None:
                deadline = asyncio.get_event_loop().time() + timeout
                while asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    try:
                        params = await self.wait_for_event(
                            "Network.requestWillBeSent",
                            session_id=session_id,
                            timeout=remaining,
                        )
                    except TimeoutError:
                        return

                    if params.get("type") != "Document":
                        continue

                    request = params.get("request") or {}
                    request_url = str(request.get("url") or "")
                    if request_url.rstrip("/") != url.rstrip("/"):
                        continue

                    picked = pick_browser_headers(request.get("headers") or {})
                    if picked and not headers_found.done():
                        headers_found.set_result(picked)
                        return

            collector = asyncio.create_task(collect_document_headers())
            try:
                nav = await self.call(
                    "Page.navigate",
                    {"url": url},
                    session_id=session_id,
                    timeout=timeout,
                )
                if nav.get("errorText"):
                    raise RuntimeError(f"navigation failed: {nav['errorText']}")

                try:
                    captured = await asyncio.wait_for(headers_found, timeout=timeout)
                except TimeoutError:
                    captured = {}
            finally:
                collector.cancel()
                try:
                    await collector
                except asyncio.CancelledError:
                    pass

            js_headers = pick_browser_headers(
                await self.evaluate(_CAPTURE_HEADERS_JS, session_id) or {}
            )
            for key, value in js_headers.items():
                captured.setdefault(key, value)
            return pick_browser_headers(captured)
        finally:
            if target_id:
                try:
                    await self.call("Target.closeTarget", {"targetId": target_id})
                except Exception:
                    pass

    async def evaluate(self, expression: str, session_id: str) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            session_id=session_id,
        )
        if isinstance(result, dict) and result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            exc = (detail.get("exception") or {}) if isinstance(detail, dict) else {}
            text = (
                (detail.get("text") if isinstance(detail, dict) else None)
                or exc.get("description")
                or exc.get("value")
                or detail
            )
            raise RuntimeError(text)
        inner = (result or {}).get("result") or {}
        if inner.get("subtype") == "error":
            raise RuntimeError(inner.get("description") or inner)
        return inner.get("value")

    async def _focus_target(self, target_id: str, session_id: str) -> None:
        """Bring the tab to the foreground so the OS clicker can see Turnstile.

        Skipped when more than one cookie-API job can run: two windows on one
        X display steal focus, and the OS clicker then hits the wrong page.
        Parallel solves click the widget with CDP ``Input.dispatchMouseEvent``
        on this target session instead.
        """
        if not self._focus_windows:
            return
        try:
            await self.call("Target.activateTarget", {"targetId": target_id})
        except Exception:
            pass
        try:
            await self.call("Page.bringToFront", session_id=session_id)
        except Exception:
            pass

    async def _reset_work_tab(self) -> None:
        if self._work_target_id:
            try:
                await self.call("Target.closeTarget", {"targetId": self._work_target_id})
            except Exception:
                pass
        self._work_target_id = None
        self._work_session_id = None

    async def _ensure_work_tab(self) -> tuple[str, str]:
        if self._work_target_id and self._work_session_id:
            try:
                await self.evaluate("1", self._work_session_id)
                await self._focus_target(self._work_target_id, self._work_session_id)
                return self._work_target_id, self._work_session_id
            except Exception:
                await self._reset_work_tab()

        created = await self.call("Target.createTarget", {"url": "about:blank"})
        target_id = created["targetId"]
        attached = await self.call(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        session_id = attached["sessionId"]
        await self.call("Page.enable", session_id=session_id)
        await self.call("Network.enable", session_id=session_id)
        await self._focus_target(target_id, session_id)
        self._work_target_id = target_id
        self._work_session_id = session_id
        return target_id, session_id

    async def _submit_turnstile_form(self, session_id: str) -> bool:
        result = await self.evaluate(_SUBMIT_TURNSTILE_JS, session_id)
        if not isinstance(result, dict):
            return False
        if result.get("submitted"):
            sys.stderr.write(
                "[cdp_browser] submitted turnstile verification form "
                f"via {result.get('via', 'unknown')}\n"
            )
            return True
        return False

    async def _click_at(self, session_id: str, x: float, y: float) -> None:
        for event_type, pressed in (
            ("mouseMoved", None),
            ("mousePressed", True),
            ("mouseReleased", False),
        ):
            params: dict[str, Any] = {
                "type": event_type,
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            }
            if pressed is not None:
                params["buttons"] = 1 if pressed else 0
            await self.call("Input.dispatchMouseEvent", params, session_id=session_id)

    async def _unique_click_at(
        self,
        session_id: str,
        x: float,
        y: float,
        spread: float,
        used: set[tuple[str, str]],
        rng: random.Random,
    ) -> None:
        """Click near (x, y) with a pointer position that has not been used yet.

        The image widget stores clientX/clientY and the provider rejects a
        solution whose pointer samples repeat an identical (x, y) pair.
        """
        jx = float(x)
        jy = float(y)
        for _ in range(24):
            jx = float(x) + rng.uniform(-spread, spread)
            jy = float(y) + rng.uniform(-spread, spread)
            marker = (f"{jx:.4f}", f"{jy:.4f}")
            if marker not in used:
                used.add(marker)
                await self._click_at(session_id, jx, jy)
                return
        jx = float(x) + spread + rng.uniform(0.11, 0.37)
        jy = float(y) - spread - rng.uniform(0.11, 0.37)
        used.add((f"{jx:.4f}", f"{jy:.4f}"))
        await self._click_at(session_id, jx, jy)

    async def _click_coords(self, session_id: str, coords: Any) -> bool:
        if not isinstance(coords, dict):
            return False
        try:
            x = float(coords["x"])
            y = float(coords["y"])
        except (KeyError, TypeError, ValueError):
            return False
        await self._click_at(session_id, x, y)
        return True

    async def _click_turnstile(self, session_id: str) -> bool:
        coords = await self.evaluate(_TURNSTILE_IFRAME_JS, session_id)
        return await self._click_coords(session_id, coords)

    async def _click_procaptcha(self, session_id: str, coords: Any = None) -> bool:
        if coords is None:
            snapshot = await self.evaluate(_PAGE_SNAPSHOT_JS, session_id) or {}
            coords = procaptcha_state(snapshot).get("click")
        return await self._click_coords(session_id, coords)

    async def _execute_procaptcha(self, session_id: str) -> bool:
        result = await self.evaluate(_PROSOPO_EXECUTE_JS, session_id)
        return bool(isinstance(result, dict) and result.get("executed"))

    async def _drag_path(
        self,
        session_id: str,
        points: Sequence[tuple[float, float]],
        *,
        step_delay: float = 0.008,
    ) -> bool:
        if len(points) < 2:
            return False
        start_x, start_y = points[0]
        await self.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseMoved",
                "x": start_x,
                "y": start_y,
                "button": "left",
                "buttons": 0,
            },
            session_id=session_id,
        )
        await asyncio.sleep(0.04)
        await self.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": start_x,
                "y": start_y,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            },
            session_id=session_id,
        )
        await asyncio.sleep(0.03)
        for x, y in points[1:]:
            await self.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "buttons": 1,
                },
                session_id=session_id,
            )
            if step_delay > 0:
                await asyncio.sleep(step_delay)
        end_x, end_y = points[-1]
        await asyncio.sleep(0.04)
        await self.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": end_x,
                "y": end_y,
                "button": "left",
                "buttons": 0,
                "clickCount": 1,
            },
            session_id=session_id,
        )
        return True

    async def _solve_procaptcha_puzzle(
        self,
        session_id: str,
        state: dict[str, Any] | None = None,
    ) -> tuple[float, float] | None:
        if not _PUZZLE_MATCH_AVAILABLE:
            return None
        try:
            extracted = await self.evaluate(_PROSOPO_PUZZLE_EXTRACT_JS, session_id)
        except Exception as exc:
            sys.stderr.write(
                f"[cdp_browser] procaptcha puzzle extract failed: {exc}\n"
            )
            return None
        if not isinstance(extracted, dict) or not extracted.get("ready"):
            reason = (
                extracted.get("reason")
                if isinstance(extracted, dict)
                else "no_extract"
            )
            sys.stderr.write(
                f"[cdp_browser] procaptcha puzzle images not ready ({reason})\n"
            )
            return None
        try:
            background = decode_image(str(extracted.get("background") or ""))
            piece = decode_image(str(extracted.get("piece") or ""))
            origin = (
                float(extracted.get("originX")),
                float(extracted.get("originY")),
            )
        except Exception as exc:
            sys.stderr.write(
                f"[cdp_browser] procaptcha puzzle decode failed: {exc}\n"
            )
            return None
        try:
            match = match_piece(background, piece, origin)
        except Exception as exc:
            sys.stderr.write(
                f"[cdp_browser] procaptcha puzzle match failed: {exc}\n"
            )
            return None
        board = board_rect_from_state(extracted) or board_rect_from_state(state or {})
        if board is None:
            sys.stderr.write("[cdp_browser] procaptcha puzzle has no board rect\n")
            return None
        # Entrance animation scales the panel from 0.9 to 1. The widget
        # converts clientX - boardLeft into local pixels with no scale
        # factor, so a drag during that animation (or at browser zoom)
        # must move by the unscaled image delta from the press point.
        if board.width < 280 or board.height < 185:
            sys.stderr.write(
                "[cdp_browser] procaptcha puzzle board still scaling "
                f"({board.width:.1f}x{board.height:.1f})\n"
            )
            return None
        piece_rect = extracted.get("pieceRect") if isinstance(extracted, dict) else None
        if isinstance(piece_rect, dict):
            try:
                press = (
                    float(piece_rect["left"]) + float(piece_rect["width"]) / 2.0,
                    float(piece_rect["top"]) + float(piece_rect["height"]) / 2.0,
                )
            except (KeyError, TypeError, ValueError):
                press = board.to_viewport(origin[0], origin[1])
        else:
            press = board.to_viewport(origin[0], origin[1])
        image_path = human_drag_path(origin[0], origin[1], match.x, match.y)
        path = [
            (press[0] + (x - origin[0]), press[1] + (y - origin[1]))
            for x, y in image_path
        ]
        if len(path) < 2:
            path = [
                press,
                (press[0] + (match.x - origin[0]), press[1] + (match.y - origin[1])),
            ]
        sys.stderr.write(
            "[cdp_browser] procaptcha puzzle match "
            f"origin=({origin[0]:.1f},{origin[1]:.1f}) "
            f"target=({match.x:.1f},{match.y:.1f}) "
            f"score={match.score:.3f} ncc={match.ncc:.3f} "
            f"edge={match.ncc_edge:.3f} dark={match.darkness:.3f}\n"
        )
        dragged = await self._drag_path(session_id, path)
        if dragged:
            sys.stderr.write(
                "[cdp_browser] dragged procaptcha puzzle piece via CDP\n"
            )
            return (match.x, match.y)
        return None

    async def _screenshot_png(self, session_id: str) -> Any:
        result = await self.call(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True},
            session_id=session_id,
        )
        data = (result or {}).get("data")
        if not data or decode_image is None:
            return None
        return decode_image("data:image/png;base64," + str(data))

    def _crop_tile(self, screenshot: Any, tile: dict[str, Any], dpr: float) -> Any:
        try:
            left = float(tile["x"]) * dpr
            top = float(tile["y"]) * dpr
            width = float(tile["width"]) * dpr
            height = float(tile["height"]) * dpr
        except (KeyError, TypeError, ValueError):
            return None
        if screenshot is None or width < 8 or height < 8:
            return None
        x0 = max(0, int(round(left)))
        y0 = max(0, int(round(top)))
        x1 = min(int(screenshot.shape[1]), int(round(left + width)))
        y1 = min(int(screenshot.shape[0]), int(round(top + height)))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        return screenshot[y0:y1, x0:x1]

    async def _solve_procaptcha_image(
        self,
        session_id: str,
        skip_key: str | None = None,
    ) -> str | None:
        if not _IMAGE_CAPTCHA_AVAILABLE or classify_tiles is None:
            return None
        try:
            extracted = await self.evaluate(_PROSOPO_IMAGE_EXTRACT_JS, session_id)
        except Exception as exc:
            sys.stderr.write(
                f"[cdp_browser] procaptcha image extract failed: {exc}\n"
            )
            return None
        if not isinstance(extracted, dict) or not extracted.get("ready"):
            return None
        label = str(extracted.get("label") or "").strip()
        tiles = extracted.get("tiles")
        if not label or not isinstance(tiles, list) or len(tiles) < 9:
            return None
        round_key = str(extracted.get("roundKey") or label)
        if skip_key is not None and round_key == skip_key:
            return round_key
        try:
            dpr = float(
                await self.evaluate("window.devicePixelRatio || 1", session_id) or 1
            )
        except Exception:
            dpr = 1.0
        screenshot = await self._screenshot_png(session_id)
        if screenshot is None:
            return None
        images = []
        usable: list[dict[str, Any]] = []
        for tile in tiles:
            if not isinstance(tile, dict):
                continue
            crop = self._crop_tile(screenshot, tile, dpr)
            if crop is None:
                continue
            rgb = crop[..., :3] if getattr(crop, "ndim", 0) == 3 else crop
            try:
                variance = float(rgb.astype("float32").std())
            except (AttributeError, TypeError, ValueError):
                variance = 0.0
            if variance < 4.0:
                continue
            images.append(crop)
            usable.append(tile)
        if len(images) < 9:
            return None
        try:
            scores = classify_tiles(label, images)
        except Exception as exc:
            sys.stderr.write(
                f"[cdp_browser] procaptcha image classify failed: {exc}\n"
            )
            return None
        chosen = [item.index for item in scores if item.selected]
        sys.stderr.write(
            "[cdp_browser] procaptcha image "
            f"label={label!r} select={chosen} "
            f"scores=[{', '.join(f'{item.score:.3f}' for item in scores)}]\n"
        )
        jitter = random.Random()
        used: set[tuple[str, str]] = set()
        for item in scores:
            if item.index >= len(usable):
                continue
            tile = usable[item.index]
            already = bool(tile.get("selected"))
            if already == item.selected:
                continue
            click = tile.get("click") if isinstance(tile.get("click"), dict) else tile
            try:
                x = float(click["x"])
                y = float(click["y"])
            except (KeyError, TypeError, ValueError):
                continue
            await self._unique_click_at(session_id, x, y, 6.0, used, jitter)
            await asyncio.sleep(jitter.uniform(0.18, 0.42))
        nxt = extracted.get("next")
        if isinstance(nxt, dict):
            await asyncio.sleep(jitter.uniform(0.25, 0.55))
            try:
                await self._unique_click_at(
                    session_id,
                    float(nxt["x"]),
                    float(nxt["y"]),
                    4.0,
                    used,
                    jitter,
                )
            except (KeyError, TypeError, ValueError):
                pass
        return round_key

    async def _solve_procaptcha_icon_order(
        self,
        session_id: str,
        skip_key: str | None = None,
    ) -> str | None:
        if not _ICON_ORDER_AVAILABLE or match_icon_order is None:
            return None
        try:
            extracted = await self.evaluate(_PROSOPO_ICON_EXTRACT_JS, session_id)
        except Exception as exc:
            sys.stderr.write(
                f"[cdp_browser] procaptcha icon-order extract failed: {exc}\n"
            )
            return None
        if not isinstance(extracted, dict) or not extracted.get("ready"):
            return None
        round_key = str(extracted.get("roundKey") or "icon")
        if skip_key is not None and round_key == skip_key:
            return round_key
        try:
            board, legend = decode_icon_images(
                extracted.get("board") or "",
                extracted.get("legend") or "",
            )
        except Exception as exc:
            sys.stderr.write(
                f"[cdp_browser] procaptcha icon-order decode failed: {exc}\n"
            )
            return None
        size = extracted.get("legendIconSize")
        try:
            legend_size = int(size) if size else None
        except (TypeError, ValueError):
            legend_size = None
        hits = match_icon_order(board, legend, legend_icon_size=legend_size)
        frame = extracted.get("frame") if isinstance(extracted.get("frame"), dict) else {}
        try:
            left = float(frame["left"])
            top = float(frame["top"])
            width = float(frame["width"])
            height = float(frame["height"])
        except (KeyError, TypeError, ValueError):
            return None
        if width < 80 or height < 60 or not hits:
            return None
        sys.stderr.write(
            "[cdp_browser] procaptcha icon-order "
            + " ".join(f"({hit.x:.1f},{hit.y:.1f}:{hit.score:.2f})" for hit in hits)
            + "\n"
        )
        jitter = random.Random()
        used: set[tuple[str, str]] = set()
        for hit in hits:
            x = left + (hit.x / ICON_BOARD_WIDTH) * width
            y = top + (hit.y / ICON_BOARD_HEIGHT) * height
            await self._unique_click_at(session_id, x, y, 2.5, used, jitter)
            await asyncio.sleep(jitter.uniform(0.16, 0.38))
        ok = extracted.get("ok")
        if isinstance(ok, dict):
            await asyncio.sleep(jitter.uniform(0.22, 0.48))
            try:
                await self._unique_click_at(
                    session_id,
                    float(ok["x"]),
                    float(ok["y"]),
                    3.0,
                    used,
                    jitter,
                )
            except (KeyError, TypeError, ValueError):
                pass
        return round_key

    async def _wait_for_page_clearance(
        self,
        target_id: str,
        session_id: str,
        timeout: float,
    ) -> tuple[dict[str, Any], bool]:
        loop = asyncio.get_event_loop()
        started = loop.time()
        max_deadline = started + (2 * timeout)
        last_href = ""
        last_focus = 0.0
        last_cdp_click = 0.0
        last_procaptcha_click = 0.0
        last_puzzle_drag: float | None = None
        last_submit = 0.0
        last_reload = 0.0
        stuck_challenge_since: float | None = None
        reload_count = 0
        max_reloads = 3
        procaptcha_executed = False
        logged_visual = False
        last_puzzle_key: tuple[int, int, int] | None = None
        last_drag_dest: tuple[float, float] | None = None
        puzzle_ready_at = 0.0
        last_image_key: str | None = None
        last_image_solve: float | None = None
        image_ready_at = 0.0
        last_icon_key: str | None = None
        last_icon_solve: float | None = None
        icon_ready_at = 0.0

        while loop.time() < max_deadline:
            now = loop.time()
            if self._focus_windows and now - last_focus >= 2.0:
                await self._focus_target(target_id, session_id)
                last_focus = now

            snapshot = await self.evaluate(_PAGE_SNAPSHOT_JS, session_id) or {}
            title = str(snapshot.get("title") or "")
            href = str(snapshot.get("href") or "")
            ready = str(snapshot.get("ready") or "")
            html = str(snapshot.get("html") or "")

            if href and href != last_href:
                if last_href:
                    try:
                        remaining = max_deadline - loop.time()
                        if remaining > 0:
                            await self.wait_for_event(
                                "Page.loadEventFired",
                                session_id=session_id,
                                timeout=remaining,
                            )
                    except TimeoutError:
                        pass
                    await self._focus_target(target_id, session_id)
                    snapshot = await self.evaluate(_PAGE_SNAPSHOT_JS, session_id) or {}
                    title = str(snapshot.get("title") or "")
                    href = str(snapshot.get("href") or "")
                    ready = str(snapshot.get("ready") or "")
                    html = str(snapshot.get("html") or "")
                    stuck_challenge_since = None
                    procaptcha_executed = False
                last_href = href

            on_cf = is_challenge_page(title, href, html)
            on_prosopo = pending_procaptcha(snapshot)
            on_challenge = on_cf or on_prosopo
            if on_cf:
                if stuck_challenge_since is None:
                    stuck_challenge_since = now
                elif (
                    now - stuck_challenge_since >= 12.0
                    and reload_count < max_reloads
                    and now - last_reload >= 12.0
                ):
                    sys.stderr.write("[cdp_browser] challenge stuck, reloading tab\n")
                    await self.call("Page.reload", session_id=session_id)
                    reload_count += 1
                    last_reload = now
                    stuck_challenge_since = None
                    last_href = ""
                    procaptcha_executed = False
                    try:
                        await self.wait_for_event(
                            "Page.loadEventFired",
                            session_id=session_id,
                            timeout=min(30.0, max_deadline - loop.time()),
                        )
                    except TimeoutError:
                        pass
                    continue

                if now - last_submit >= 1.0:
                    if await self._submit_turnstile_form(session_id):
                        last_submit = now
                        stuck_challenge_since = None
                        try:
                            await self.wait_for_event(
                                "Page.loadEventFired",
                                session_id=session_id,
                                timeout=min(30.0, max_deadline - loop.time()),
                            )
                        except TimeoutError:
                            pass
                        continue

                if now - last_cdp_click >= 2.5:
                    if await self._click_turnstile(session_id):
                        sys.stderr.write("[cdp_browser] clicked turnstile widget via CDP\n")
                    last_cdp_click = now
            else:
                stuck_challenge_since = None

            if on_prosopo:
                state = procaptcha_state(snapshot)
                kind = str(state.get("kind") or "")
                if kind == "puzzle":
                    try:
                        origin_x = float(state.get("originX") or 0)
                        origin_y = float(state.get("originY") or 0)
                        puzzle_key = (
                            int(round(origin_x)),
                            int(round(origin_y)),
                            int(round(float(state.get("pieceSize") or 0))),
                        )
                    except (TypeError, ValueError):
                        origin_x = 0.0
                        origin_y = 0.0
                        puzzle_key = (0, 0, 0)
                    sitting_on_drop = (
                        last_drag_dest is not None
                        and math.hypot(
                            origin_x - last_drag_dest[0],
                            origin_y - last_drag_dest[1],
                        )
                        < 22
                    )
                    if sitting_on_drop:
                        pass
                    else:
                        if last_puzzle_key != puzzle_key:
                            last_puzzle_key = puzzle_key
                            # Entrance scale(0.9)→scale(1) is 0.3s; wait it out
                            # so clientX maps 1:1 onto the 300×200 board.
                            puzzle_ready_at = now + 0.45
                        cooled = (
                            last_puzzle_drag is None
                            or now - last_puzzle_drag >= 1.2
                        )
                        if now >= puzzle_ready_at and cooled:
                            dest = await self._solve_procaptcha_puzzle(
                                session_id, state
                            )
                            if dest:
                                last_puzzle_drag = now
                                last_drag_dest = dest
                                puzzle_ready_at = now + 4.0
                elif kind == "image":
                    if image_ready_at == 0.0:
                        image_ready_at = now + 0.45
                    cooled = (
                        last_image_solve is None
                        or now - last_image_solve >= 1.6
                    )
                    if now >= image_ready_at and cooled:
                        key = await self._solve_procaptcha_image(
                            session_id, skip_key=last_image_key
                        )
                        if key:
                            last_image_key = key
                            last_image_solve = now
                            image_ready_at = now + 1.8
                elif kind == "icon_order":
                    if icon_ready_at == 0.0:
                        icon_ready_at = now + 0.45
                    cooled = (
                        last_icon_solve is None
                        or now - last_icon_solve >= 2.2
                    )
                    if now >= icon_ready_at and cooled:
                        key = await self._solve_procaptcha_icon_order(
                            session_id, skip_key=last_icon_key
                        )
                        if key:
                            last_icon_key = key
                            last_icon_solve = now
                            icon_ready_at = now + 2.5
                elif kind == "visual":
                    if not logged_visual:
                        sys.stderr.write(
                            "[cdp_browser] procaptcha unknown visual challenge; "
                            "leaving unsolved\n"
                        )
                        logged_visual = True
                    return snapshot, True
                elif kind == "invisible":
                    if not procaptcha_executed:
                        if await self._execute_procaptcha(session_id):
                            sys.stderr.write(
                                "[cdp_browser] executed invisible procaptcha widget\n"
                            )
                        procaptcha_executed = True
                elif state.get("click") and now - last_procaptcha_click >= 2.5:
                    if await self._click_procaptcha(session_id, state.get("click")):
                        sys.stderr.write(
                            "[cdp_browser] clicked procaptcha widget via CDP\n"
                        )
                    last_procaptcha_click = now

            if ready == "complete" and not on_challenge:
                return snapshot, False
            await asyncio.sleep(0.5)

        snapshot = await self.evaluate(_PAGE_SNAPSHOT_JS, session_id) or {}
        title = str(snapshot.get("title") or "")
        href = str(snapshot.get("href") or "")
        html = str(snapshot.get("html") or "")
        timed_out = is_challenge_page(title, href, html) or pending_procaptcha(snapshot)
        return snapshot, timed_out

    async def _wait_minimum(
        self,
        session_id: str,
        min_wait: float,
        deadline: float,
    ) -> None:
        if min_wait <= 0:
            return

        loop = asyncio.get_event_loop()
        wait_end = loop.time() + min_wait
        last_href = await self.evaluate("location.href || ''", session_id) or ""

        while loop.time() < wait_end:
            if loop.time() >= deadline:
                return
            await asyncio.sleep(0.25)
            href = await self.evaluate("location.href || ''", session_id) or ""
            if href == last_href:
                continue
            last_href = href
            try:
                remaining = min(min_wait, deadline - loop.time())
                if remaining > 0:
                    await self.wait_for_event(
                        "Page.loadEventFired",
                        session_id=session_id,
                        timeout=remaining,
                    )
            except TimeoutError:
                pass
            wait_end = loop.time() + min_wait

    async def fetch_cookies(
        self,
        url: str,
        timeout: float = 60.0,
        min_wait: float = 0.0,
    ) -> list[dict]:
        target_id, session_id = await self._ensure_work_tab()
        await self._focus_target(target_id, session_id)

        nav = await self.call(
            "Page.navigate",
            {"url": url},
            session_id=session_id,
            timeout=timeout,
        )
        if nav.get("errorText"):
            raise RuntimeError(f"navigation failed: {nav['errorText']}")

        try:
            await self.wait_for_event(
                "Page.loadEventFired",
                session_id=session_id,
                timeout=timeout,
            )
        except TimeoutError:
            pass

        await self._focus_target(target_id, session_id)
        _, timed_out_on_challenge = await self._wait_for_page_clearance(
            target_id,
            session_id,
            timeout,
        )
        loop = asyncio.get_event_loop()
        max_deadline = loop.time() + timeout
        if not timed_out_on_challenge:
            await self._wait_minimum(session_id, min_wait, max_deadline)

        final_url = await self.evaluate("location.href || ''", session_id) or url
        cookies_result = await self.call(
            "Network.getCookies",
            {"urls": [url, final_url]},
            session_id=session_id,
        )
        return list(cookies_result.get("cookies") or [])

    async def _fetch_response_body(
        self,
        request_id: str | None,
        session_id: str,
    ) -> bytes:
        if not request_id:
            return b""
        try:
            body_result = await self.call(
                "Network.getResponseBody",
                {"requestId": request_id},
                session_id=session_id,
            )
        except Exception:
            return b""

        body = body_result.get("body") or ""
        if body_result.get("base64Encoded"):
            return base64.b64decode(body)
        if isinstance(body, str):
            return body.encode("utf-8")
        return bytes(body)

    async def fetch_page(
        self,
        url: str,
        timeout: float = 60.0,
        min_wait: float = 0.0,
    ) -> dict[str, Any]:
        """Load ``url`` and return status, headers, body, final URL, and cookies."""
        target_id, session_id = await self._ensure_work_tab()
        await self._focus_target(target_id, session_id)

        document_responses: list[dict[str, Any]] = []

        async def collect_document_responses() -> None:
            deadline = asyncio.get_event_loop().time() + (2 * timeout) + min_wait + 5.0
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                try:
                    params = await self.wait_for_event(
                        "Network.responseReceived",
                        session_id=session_id,
                        timeout=min(remaining, 5.0),
                    )
                except TimeoutError:
                    if asyncio.get_event_loop().time() >= deadline:
                        return
                    continue

                if params.get("type") != "Document":
                    continue

                response = params.get("response") or {}
                document_responses.append(
                    {
                        "status": response.get("status", 200),
                        "headers": dict(response.get("headers") or {}),
                        "url": str(response.get("url") or ""),
                        "requestId": params.get("requestId"),
                    }
                )

        collector = asyncio.create_task(collect_document_responses())
        try:
            nav = await self.call(
                "Page.navigate",
                {"url": url},
                session_id=session_id,
                timeout=timeout,
            )
            if nav.get("errorText"):
                raise RuntimeError(f"navigation failed: {nav['errorText']}")

            try:
                await self.wait_for_event(
                    "Page.loadEventFired",
                    session_id=session_id,
                    timeout=timeout,
                )
            except TimeoutError:
                pass

            await self._focus_target(target_id, session_id)
            snapshot, timed_out_on_challenge = await self._wait_for_page_clearance(
                target_id,
                session_id,
                timeout,
            )
            loop = asyncio.get_event_loop()
            max_deadline = loop.time() + timeout
            if not timed_out_on_challenge:
                await self._wait_minimum(session_id, min_wait, max_deadline)

            title = str(snapshot.get("title") or "")
            final_url = str(snapshot.get("href") or "") or url
            html = str(snapshot.get("html") or "")
            token = procaptcha_token(snapshot)

            doc: dict[str, Any] = {}
            if document_responses:
                for candidate in reversed(document_responses):
                    if candidate["url"].rstrip("/") == final_url.rstrip("/"):
                        doc = candidate
                        break
                if not doc:
                    doc = document_responses[-1]

            status_code = int(doc.get("status") or 200)
            headers = dict(doc.get("headers") or {})
            network_content = await self._fetch_response_body(
                doc.get("requestId"), session_id
            )
            network_len = len(network_content)
            outer_html = html
            outer_len = len(outer_html.encode("utf-8"))
            bodies_differ = bool(
                network_content
                and outer_html
                and network_content != outer_html.encode("utf-8")
            )

            if (
                network_content
                and is_interstitial_body(network_content)
                and outer_html
                and bodies_differ
            ):
                content = outer_html.encode("utf-8")
            elif token and outer_html:
                content = outer_html.encode("utf-8")
            elif not network_content and outer_html:
                content = outer_html.encode("utf-8")
            else:
                content = network_content

            still_challenge = is_challenge_page(
                title, final_url, outer_html, body_len=len(content)
            ) or pending_procaptcha(snapshot)
            if timed_out_on_challenge or still_challenge:
                sys.stderr.write(
                    "[cdp_browser] fetch_page: challenge still present at return "
                    f"(timed_out={timed_out_on_challenge})\n"
                )

            sys.stderr.write(
                "[cdp_browser] fetch_page return: "
                f"href={final_url!r} title={title!r} "
                f"network_len={network_len} outer_len={outer_len} "
                f"bodies_differ={bodies_differ} "
                f"challenge={still_challenge} "
                f"procaptcha={bool(token)} "
                f"kind={procaptcha_state(snapshot).get('kind') or '-'}\n"
            )

            cookies_result = await self.call(
                "Network.getCookies",
                {"urls": [url, final_url]},
                session_id=session_id,
            )
            cookies = list(cookies_result.get("cookies") or [])

            return {
                "url": url,
                "final_url": final_url,
                "status_code": status_code,
                "headers": headers,
                "content": content,
                "cookies": cookies,
                "redirects": document_responses[:-1] if len(document_responses) > 1 else [],
            }
        finally:
            collector.cancel()
            try:
                await collector
            except asyncio.CancelledError:
                pass


def default_browser_headers_probe_url() -> str:
    port = os.environ.get("FILE_SERVER_PORT", "9377")
    override = os.environ.get("BROWSER_HEADERS_PROBE_URL")
    if override:
        return override
    return f"http://127.0.0.1:{port}/"


async def capture_browser_headers(
    cdp_http: str,
    probe_url: str | None = None,
    timeout: float = 30.0,
) -> dict[str, str]:
    probe = probe_url or default_browser_headers_probe_url()
    await wait_for_cdp(cdp_http, timeout=timeout)
    browser = await CdpBrowser.connect(cdp_http)
    try:
        headers = await browser.capture_document_headers(probe, timeout=timeout)
    finally:
        await browser.close()

    if "User-Agent" not in headers:
        headers["User-Agent"] = get_browser_user_agent(cdp_http)
    return pick_browser_headers(headers)


def save_browser_headers(path: str, headers: dict[str, str]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pick_browser_headers(headers), fh, indent=2, sort_keys=True)
        fh.write("\n")


def load_browser_headers(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return pick_browser_headers({str(key): str(value) for key, value in data.items()})


async def wait_for_cdp(cdp_http: str, timeout: float = 30.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    last: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            discover_browser_ws(cdp_http)
            return
        except Exception as exc:
            last = exc
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Chrome CDP not reachable at {cdp_http}: {last}")


def default_cdp_http() -> str:
    return os.environ.get("CDP_URL", "http://127.0.0.1:9222")
