"""Shoot-type modes: Canon EXIF auto-detection (decision tree from genre_presets.md Q0) and mode helpers.

``detect_mode(meta, image=None)`` returns ``{mode, confidence, reasons, variant?, candidates, notes}``. Every rule
adds a candidate ``(mode, confidence, reason)``; the best one wins and the runner-ups are reported. Order of
trust (high to low): Canon ``EasyMode`` (SCN mode dial) -> Picture Style -> AF/drive/shooting-parameter
heuristics -> optional image content (dark histogram, faces, sky fraction, vertical lines).

Several MakerNote semantics are unverified on a real R50 file (see the spec): whether ``EasyMode`` is written
for every SCN mode and the unit of ``FocusDistanceUpper``. The rules are deliberately tolerant of both.
"""
from __future__ import annotations

from typing import Any

import numpy as np

AUTO_MIN_CONFIDENCE = 0.30

MODE_NAMES = ["portrait", "animal", "landscape", "night", "macro", "street", "product", "sports", "architecture",
              "events", "video"]

# ExifTool Canon CameraSettings:EasyMode values (genre_presets.md Q0). Values for the R50's SCN set that
# ExifTool does not enumerate are simply absent -> no EasyMode hint.
EASY_MODE = {
    2: ("landscape", None, "Landscape SCN"), 5: ("night", None, "Night SCN"), 8: ("portrait", None, "Portrait SCN"),
    9: ("sports", None, "Sports SCN"), 10: ("macro", None, "Close-up SCN"), 17: ("macro", None, "Super macro"),
    40: ("night", None, "Low light"), 49: ("sports", None, "High-speed burst"),
    51: ("architecture", None, "HDR backlight (3-shot JPEG composite)"),
    52: ("night", None, "Handheld night scene (4-shot JPEG composite)"),
    57: ("street", None, "Monochrome"), 61: ("portrait", None, "Smooth skin"), 62: ("portrait", None, "Soft focus"),
    68: ("product", "food", "Food SCN"), 84: ("landscape", None, "HDR art standard"),
    85: ("landscape", None, "HDR art vivid"), 93: ("landscape", None, "HDR art bold"),
    261: ("landscape", None, "Sunset"), 263: ("night", None, "Night scene"),
}
COMPOSITE_EASY = {51: "HDR Backlight Control", 52: "Handheld Night Scene"}

# PictureStyle codes (ExifTool, 0x81..0x88)
PICTURE_STYLE = {0x81: "standard", 0x82: "portrait", 0x83: "landscape", 0x84: "neutral", 0x85: "faithful",
                 0x86: "monochrome", 0x87: "auto", 0x88: "fine_detail"}
PICTURE_STYLE_MODE = {0x82: ("portrait", 0.55), 0x83: ("landscape", 0.55), 0x86: ("street", 0.50),
                      0x84: ("product", 0.30), 0x85: ("product", 0.33), 0x88: ("macro", 0.30)}

FOCUS_AI_SERVO = 1
AF_FACE_DETECT = 0x4006
DRIVE_BURST = {1, 4, 5, 8, 10}   # continuous / low / high / high+ / continuous silent


def _num(meta: dict, key: str, default=None):
    v = meta.get(key)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _hour(meta: dict) -> int | None:
    dt = meta.get("datetime")
    if not dt:
        return None
    try:
        return int(str(dt)[11:13])
    except ValueError:
        return None


def image_features(image: np.ndarray | None) -> dict:
    """Cheap content signals from a display-referred or linear float image (any size)."""
    if image is None:
        return {}
    import cv2

    img = np.asarray(image, np.float32)
    s = 320.0 / max(img.shape[:2])
    if s < 1:
        img = cv2.resize(img, (max(1, round(img.shape[1] * s)), max(1, round(img.shape[0] * s))),
                         interpolation=cv2.INTER_AREA)
    img = np.clip(img, 0, 1)
    lum = img @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    feats: dict[str, Any] = {"median_lum": float(np.median(lum)), "dark_frac": float((lum < 0.08).mean()),
                             "bright_frac": float((lum > 0.92).mean())}
    h = img.shape[0]
    top = img[: max(1, h // 3)]
    blue = (top[..., 2] > top[..., 0] * 1.08) & (top[..., 2] > 0.35) & (top.mean(-1) > 0.4)
    cloud = (top.min(-1) > 0.75)
    feats["sky_frac"] = float((blue | cloud).mean())
    # vertical line strength (architecture): fraction of strong near-vertical gradient edges
    g = cv2.cvtColor((img * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)
    strong = mag > 80
    if strong.sum() > 50:
        feats["vertical_edge_ratio"] = float(((np.abs(gx) > 2.5 * np.abs(gy)) & strong).sum() / strong.sum())
        feats["horizontal_edge_ratio"] = float(((np.abs(gy) > 2.5 * np.abs(gx)) & strong).sum() / strong.sum())
    feats.update(_faces(g))
    return feats


def _faces(gray: np.ndarray) -> dict:
    """Optional OpenCV Haar frontal-face detection (skipped silently when the cascade is unavailable)."""
    try:
        import cv2

        path = getattr(cv2, "data", None)
        if path is None:
            return {}
        casc = cv2.CascadeClassifier(path.haarcascades + "haarcascade_frontalface_default.xml")
        if casc.empty():
            return {}
        faces = casc.detectMultiScale(gray, 1.15, 5, minSize=(max(16, gray.shape[0] // 14),) * 2)
        n = len(faces)
        frac = float(max((f[3] for f in faces), default=0) / gray.shape[0])
        return {"faces": int(n), "face_frac": frac}
    except Exception:  # noqa: BLE001
        return {}


def detect_mode(meta: dict, image: np.ndarray | None = None) -> dict:
    """Guess the shoot type of a frame from Canon MakerNote/EXIF (``metadata.read_metadata`` keys) and, if
    given, the image. Returns ``{mode, confidence 0..1, reasons: [..], variant, candidates, notes}``."""
    meta = meta or {}
    cands: list[tuple[str, float, str, str | None]] = []
    notes: list[str] = []

    def add(mode, conf, reason, variant=None):
        cands.append((mode, float(conf), reason, variant))

    iso = _num(meta, "iso")
    shutter = _num(meta, "shutter")
    aperture = _num(meta, "aperture")
    focal = _num(meta, "focal")
    focal35 = _num(meta, "focal35") or (focal * 1.613 if focal else None)
    easy = meta.get("easy_mode")
    style = meta.get("picture_style")
    focus_mode = meta.get("focus_mode")
    drive = meta.get("continuous_drive")
    af_point = meta.get("af_point")
    macro_mode = meta.get("macro_mode")
    focus_range = meta.get("focus_range")
    fdist = _num(meta, "focus_distance_upper")
    faces_meta = meta.get("faces_detected")
    hour = _hour(meta)
    feats = image_features(image)

    # 1. EasyMode / SCN dial
    if isinstance(easy, int) and easy in EASY_MODE:
        mode, var, why = EASY_MODE[easy]
        add(mode, 0.93 if mode in ("portrait", "landscape", "macro", "sports") else 0.85, f"EasyMode={easy} ({why})", var)
        if easy in COMPOSITE_EASY:
            notes.append(f"{COMPOSITE_EASY[easy]} is a multi-shot in-camera JPEG composite: treat as a finished render, "
                         "keep edits gentle (no raw pipeline exists for it)")

    # 2. Picture Style hint
    if isinstance(style, int) and style in PICTURE_STYLE_MODE:
        mode, conf = PICTURE_STYLE_MODE[style]
        add(mode, conf, f"PictureStyle={PICTURE_STYLE.get(style, hex(style))}")

    # 3. continuous drive + servo AF
    servo = focus_mode == FOCUS_AI_SERVO
    burst = isinstance(drive, int) and drive in DRIVE_BURST
    if burst and servo and shutter is not None and shutter <= 1 / 500:
        add("sports", 0.72, f"burst drive + AI Servo + shutter 1/{round(1 / shutter)}")
    elif burst and servo:
        add("sports", 0.50, "burst drive + AI Servo")
    if servo and focal35 and focal35 >= 240:
        conf = 0.60 if (shutter and shutter <= 1 / 640) else 0.52
        add("animal", conf, f"AI Servo + long lens ({focal:.0f} mm, {focal35:.0f} mm eq.)" if focal else "AI Servo + long lens")
    if feats.get("faces", 0) == 0 and servo and focal35 and focal35 >= 240 and burst:
        add("animal", 0.62, "servo burst with a long lens and no face found")

    # 4. macro
    if macro_mode == 1:
        add("macro", 0.72, "MacroMode=1 (macro)")
    if isinstance(focus_range, int) and focus_range in (3, 4):
        add("macro", 0.60 if focus_range == 3 else 0.50, f"FocusRange={focus_range} ({'macro' if focus_range == 3 else 'very close'})")
    if fdist is not None and 0 < fdist <= 0.5:
        add("macro", 0.62, f"focus distance {fdist:g} (<= 0.5)")

    # 5. night / astro / blue hour
    if shutter is not None and iso is not None and shutter >= 5 and iso >= 1600:
        var = "astro" if (shutter >= 10 and (focal35 or 50) <= 40) else None
        add("night", 0.82, f"long exposure {shutter:g}s at ISO {iso:.0f}", var)
    elif shutter is not None and shutter >= 1 and iso is not None and iso >= 800 and hour is not None and (hour >= 21 or hour <= 4):
        add("night", 0.55, f"{shutter:g}s exposure at ISO {iso:.0f} after dark ({hour}:00)")
    if feats and feats.get("median_lum", 1) < 0.08 and (iso or 0) >= 1600:
        add("night", 0.60, f"dark histogram (median {feats['median_lum']:.3f}) at ISO {iso:.0f}",
            "astro" if feats.get("dark_frac", 0) > 0.8 else None)
    elif feats and feats.get("median_lum", 1) < 0.05:
        add("night", 0.45, f"very dark image (median {feats['median_lum']:.3f})")
    if hour is not None and 17 <= hour <= 19 and iso is not None and iso >= 1600 and shutter and shutter >= 1 / 30 \
            and (feats.get("median_lum", 0.2) < 0.2 if feats else False):
        add("night", 0.40, "dusk time + high ISO + dim scene", "blue_hour")

    # 6. portrait / events (faces)
    if af_point == AF_FACE_DETECT or (isinstance(af_point, int) and af_point == 16390):
        add("portrait", 0.62, "AF point type = Face Detect")
    if isinstance(faces_meta, int) and faces_meta >= 1:
        add("events" if faces_meta >= 4 else "portrait", 0.62 if faces_meta < 4 else 0.58, f"{faces_meta} face(s) in MakerNote")
    nf = feats.get("faces", 0)
    if nf:
        if nf >= 4:
            add("events", 0.58, f"{nf} faces in the image")
        elif feats.get("face_frac", 0) >= 0.10:
            add("portrait", 0.65, f"face height {feats['face_frac']:.0%} of the frame")
        else:
            add("events", 0.45, f"{nf} small face(s)")

    # 7. landscape
    if isinstance(focus_range, int) and focus_range in (7, 10) and (aperture or 0) >= 7.1:
        add("landscape", 0.45, f"far/infinity focus at f/{aperture:g}")
    if feats and feats.get("sky_frac", 0) > 0.30 and (focal35 or 0) and focal35 <= 45 and not feats.get("faces"):
        add("landscape", 0.52, f"sky covers {feats['sky_frac']:.0%} of the top third, wide lens")
    if shutter is not None and shutter >= 1 and shutter < 5 and (iso or 9999) <= 200 and (feats.get("median_lum", 0.3) > 0.12 if feats else True):
        add("landscape", 0.40, f"low-ISO long exposure ({shutter:g}s)")

    # 8. architecture
    if feats and (focal35 or 100) <= 30 and feats.get("vertical_edge_ratio", 0) > 0.55 and not feats.get("faces"):
        add("architecture", 0.50, f"wide lens ({focal35:.0f} mm eq.) and dominant vertical edges")

    if not cands:
        return {"mode": "events", "confidence": 0.2, "variant": None, "reasons": ["no strong signal: generic snapshot/travel default"],
                "candidates": [], "notes": notes, "features": feats}
    ranked = sorted(cands, key=lambda c: -c[1])
    best = ranked[0]
    # agreement bonus: several independent rules for the same mode
    agree = [c for c in ranked if c[0] == best[0]]
    conf = min(0.98, best[1] + 0.05 * (len(agree) - 1))
    variant = next((c[3] for c in agree if c[3]), None)
    reasons = [c[2] for c in agree]
    others = [{"mode": c[0], "confidence": round(c[1], 2), "reason": c[2]} for c in ranked if c[0] != best[0]][:4]
    return {"mode": best[0], "confidence": round(conf, 2), "variant": variant, "reasons": reasons,
            "candidates": others, "notes": notes, "features": feats}


def mode_preset_name(det: dict) -> str:
    """'night:astro' style preset name for a detect_mode result."""
    return det["mode"] + (":" + det["variant"] if det.get("variant") else "")
