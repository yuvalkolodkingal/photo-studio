"""Metadata: EXIF/MakerNote reading (ExifTool via PyExifTool, Pillow fallback) and standard XMP sidecars.

Rules: CR3 (and any raw) bytes are NEVER modified. Ratings/labels/keywords go to `<file>.<ext>.xmp`
sidecars (darktable/digiKam convention). JPEGs may optionally be written in place via ExifTool
(-P -overwrite_original) and are read back to verify, because ExifTool silently ignores some bad input.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import RAW_EXTS, MissingDependency, which

_exiftool_helper = None


def _et():
    """Persistent ExifToolHelper (one perl process per batch), or None if unavailable."""
    global _exiftool_helper
    if _exiftool_helper is not None:
        return _exiftool_helper
    if not which("exiftool"):
        return None
    try:
        import exiftool  # PyExifTool

        _exiftool_helper = exiftool.ExifToolHelper(common_args=["-G", "-n"])
        _exiftool_helper.run()
        return _exiftool_helper
    except Exception:
        return None


def exiftool_available() -> bool:
    return _et() is not None


def _parse_dt(s: Any) -> str | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).isoformat()
        except ValueError:
            continue
    return None


def _first(d: dict, *keys, default=None):
    for k in keys:
        for kk, v in d.items():
            if kk == k or kk.endswith(":" + k):
                if v not in (None, ""):
                    return v
    return default


def normalize(raw: dict) -> dict:
    """Map ExifTool (-G -n) / Pillow tags to the normalized keys used everywhere."""
    md = {
        "make": _first(raw, "Make"),
        "model": _first(raw, "Model"),
        "lens": _first(raw, "LensModel", "LensType", "Lens"),
        "iso": _first(raw, "ISO", "ISOSpeedRatings", "PhotographicSensitivity"),
        "shutter": _first(raw, "ExposureTime"),
        "aperture": _first(raw, "FNumber", "Aperture"),
        "focal": _first(raw, "FocalLength"),
        "focal35": _first(raw, "FocalLengthIn35mmFormat", "FocalLength35efl"),
        "exposure_comp": _first(raw, "ExposureCompensation", "ExposureBiasValue"),
        "datetime": _parse_dt(_first(raw, "DateTimeOriginal", "CreateDate", "DateTime")),
        "subsec": _first(raw, "SubSecTimeOriginal", "SubSecTime"),
        "orientation": _first(raw, "Orientation", default=1),
        "width": _first(raw, "ImageWidth", "ExifImageWidth"),
        "height": _first(raw, "ImageHeight", "ExifImageHeight"),
        "gps_lat": _first(raw, "GPSLatitude"),
        "gps_lon": _first(raw, "GPSLongitude"),
        "gps_alt": _first(raw, "GPSAltitude"),
        "serial": _first(raw, "SerialNumber", "InternalSerialNumber"),
        "file_number": _first(raw, "FileNumber", "FileIndex"),
        # Canon MakerNote (ExifTool numeric values)
        "easy_mode": _first(raw, "EasyMode"),
        "picture_style": _first(raw, "PictureStyle"),
        "focus_mode": _first(raw, "FocusMode"),
        "continuous_drive": _first(raw, "ContinuousDrive"),
        "af_point": _first(raw, "AFPoint"),
        "macro_mode": _first(raw, "MacroMode"),
        "focus_range": _first(raw, "FocusRange"),
        "focus_distance_upper": _first(raw, "FocusDistanceUpper"),
        "white_balance": _first(raw, "WhiteBalance"),
        "faces_detected": _first(raw, "FacesDetected"),
        "flash": _first(raw, "Flash"),
        "rating": _first(raw, "Rating"),
    }
    for k in ("iso", "shutter", "aperture", "focal", "focal35", "exposure_comp", "gps_lat", "gps_lon",
              "gps_alt", "focus_distance_upper"):
        v = md.get(k)
        if v is not None:
            try:
                md[k] = float(v)
            except (TypeError, ValueError):
                md[k] = None
    for k in ("easy_mode", "picture_style", "focus_mode", "continuous_drive", "af_point", "macro_mode",
              "focus_range", "orientation", "faces_detected", "white_balance", "width", "height"):
        v = md.get(k)
        if v is not None:
            try:
                md[k] = int(float(v))
            except (TypeError, ValueError):
                pass
    if md.get("subsec") is not None:
        md["subsec"] = str(md["subsec"])
    return {k: v for k, v in md.items() if v is not None}


def _pillow_exif(path: Path) -> dict:
    from PIL import ExifTags, Image

    out: dict[str, Any] = {}
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            out["ImageWidth"], out["ImageHeight"] = im.size
            for k, v in ex.items():
                out[ExifTags.TAGS.get(k, str(k))] = v
            for ifd in (ExifTags.IFD.Exif, ExifTags.IFD.GPSInfo):
                try:
                    sub = ex.get_ifd(ifd)
                except Exception:
                    continue
                for k, v in sub.items():
                    name = (ExifTags.GPSTAGS if ifd == ExifTags.IFD.GPSInfo else ExifTags.TAGS).get(k, str(k))
                    out[name] = v
    except Exception:
        return out
    # Rationals -> float; GPS dms -> decimal
    for k, v in list(out.items()):
        try:
            if hasattr(v, "numerator"):
                out[k] = float(v)
        except Exception:
            pass
    for ref, key in (("GPSLatitudeRef", "GPSLatitude"), ("GPSLongitudeRef", "GPSLongitude")):
        v = out.get(key)
        if isinstance(v, tuple) and len(v) == 3:
            dec = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
            if str(out.get(ref, "N")).upper() in ("S", "W"):
                dec = -dec
            out[key] = dec
    if "ISOSpeedRatings" in out and isinstance(out["ISOSpeedRatings"], tuple):
        out["ISOSpeedRatings"] = out["ISOSpeedRatings"][0]
    return out


def read_metadata(path: str | Path, raw_tags: bool = False) -> dict:
    """Return normalized metadata dict (plus '_raw' with every tag when raw_tags=True)."""
    path = Path(path)
    raw: dict[str, Any] = {}
    if path.suffix.lower() == ".npz":  # synthetic raw used in tests / demos
        import numpy as np

        with np.load(path, allow_pickle=False) as z:
            if "meta" in z:
                raw = json.loads(str(z["meta"]))
    else:
        et = _et()
        if et is not None:
            try:
                raw = et.get_metadata([str(path)])[0]
            except Exception:
                raw = {}
        if not raw and path.suffix.lower() not in RAW_EXTS:
            raw = _pillow_exif(path)
    md = normalize(raw)
    xmp = read_xmp(xmp_path(path))
    if xmp:
        md["xmp"] = xmp
    if raw_tags:
        md["_raw"] = raw
    return md


# --------------------------------------------------------------------------- XMP sidecars
NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "lr": "http://ns.adobe.com/lightroom/1.0/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "Iptc4xmpCore": "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/",
    "photostudio": "https://github.com/photostudio/ns/1.0/",
}
for _p, _u in NS.items():
    ET.register_namespace(_p, _u)


def xmp_path(path: str | Path) -> Path:
    p = Path(path)
    return p.with_name(p.name + ".xmp")


def read_xmp(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        tree = ET.parse(p)
    except ET.ParseError:
        return {}
    out: dict[str, Any] = {}
    for desc in tree.iter(f"{{{NS['rdf']}}}Description"):
        for attr, key in ((f"{{{NS['xmp']}}}Rating", "rating"), (f"{{{NS['xmp']}}}Label", "label"),
                          (f"{{{NS['photoshop']}}}City", "city"), (f"{{{NS['photoshop']}}}State", "state"),
                          (f"{{{NS['photoshop']}}}Country", "country"),
                          (f"{{{NS['photostudio']}}}Flags", "flags")):
            if attr in desc.attrib:
                out[key] = desc.attrib[attr]
        for tag, key in ((f"{{{NS['dc']}}}subject", "keywords"), (f"{{{NS['lr']}}}hierarchicalSubject", "hierarchical")):
            el = desc.find(tag)
            if el is not None:
                out[key] = [li.text for li in el.iter(f"{{{NS['rdf']}}}li") if li.text]
        for tag, key in ((f"{{{NS['xmp']}}}Rating", "rating"), (f"{{{NS['xmp']}}}Label", "label")):
            el = desc.find(tag)
            if el is not None and el.text:
                out[key] = el.text
    if "rating" in out:
        try:
            out["rating"] = int(float(out["rating"]))
        except ValueError:
            pass
    return out


def write_xmp(path: str | Path, rating: int | None = None, label: str | None = None,
              keywords: list[str] | None = None, hierarchical: list[str] | None = None,
              place: dict | None = None, flags: list[str] | None = None, merge: bool = True) -> Path:
    """Write/merge a standard XMP sidecar next to `path` (path is the image, not the .xmp)."""
    xp = xmp_path(path)
    cur = read_xmp(xp) if merge else {}
    if rating is not None:
        cur["rating"] = int(rating)
    if label is not None:
        cur["label"] = label
    if keywords is not None:
        cur["keywords"] = sorted(set(keywords))
    if hierarchical is not None:
        cur["hierarchical"] = sorted(set(hierarchical))
    if place:
        cur.update({k: v for k, v in place.items() if k in ("city", "state", "country") and v})
    if flags is not None:
        cur["flags"] = ",".join(sorted(set(flags)))
    root = ET.Element(f"{{{NS['x']}}}xmpmeta", {f"{{{NS['x']}}}xmptk": "photostudio"})
    rdf = ET.SubElement(root, f"{{{NS['rdf']}}}RDF")
    attrs = {f"{{{NS['rdf']}}}about": ""}
    if "rating" in cur:
        attrs[f"{{{NS['xmp']}}}Rating"] = str(cur["rating"])
    if cur.get("label"):
        attrs[f"{{{NS['xmp']}}}Label"] = str(cur["label"])
    for k, tag in (("city", "City"), ("state", "State"), ("country", "Country")):
        if cur.get(k):
            attrs[f"{{{NS['photoshop']}}}{tag}"] = str(cur[k])
    if cur.get("flags"):
        attrs[f"{{{NS['photostudio']}}}Flags"] = str(cur["flags"])
    desc = ET.SubElement(rdf, f"{{{NS['rdf']}}}Description", attrs)
    for key, tag in (("keywords", f"{{{NS['dc']}}}subject"), ("hierarchical", f"{{{NS['lr']}}}hierarchicalSubject")):
        vals = cur.get(key)
        if vals:
            el = ET.SubElement(desc, tag)
            bag = ET.SubElement(el, f"{{{NS['rdf']}}}Bag")
            for v in vals:
                ET.SubElement(bag, f"{{{NS['rdf']}}}li").text = v
    data = ET.tostring(root, encoding="unicode")
    xp.write_text('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n' + data + '\n<?xpacket end="w"?>\n',
                  encoding="utf-8")
    return xp


def write_jpeg_metadata(path: str | Path, rating: int | None = None, keywords: list[str] | None = None,
                        hierarchical: list[str] | None = None) -> bool:
    """Write rating/keywords INTO a JPEG/TIFF via ExifTool and verify by reading back.
    Refuses raw files. Returns True if verified."""
    p = Path(path)
    if p.suffix.lower() in RAW_EXTS:
        raise ValueError("Refusing to write metadata into a raw file; use write_xmp() sidecars")
    et = _et()
    if et is None:
        raise MissingDependency("exiftool not installed (sudo dnf install perl-Image-ExifTool)")
    tags: dict[str, Any] = {}
    if rating is not None:
        tags["XMP-xmp:Rating"] = int(rating)
    if keywords is not None:
        tags["XMP-dc:Subject"] = list(keywords)
        tags["IPTC:Keywords"] = list(keywords)
    if hierarchical is not None:
        tags["XMP-lr:HierarchicalSubject"] = list(hierarchical)
    if not tags:
        return True
    et.set_tags([str(p)], tags, params=["-P", "-overwrite_original"])
    back = et.get_tags([str(p)], ["XMP:Rating", "XMP:Subject"])[0]
    ok = rating is None or int(back.get("XMP:Rating", -99)) == int(rating)
    return bool(ok)


def strip_tags_for_export(path: str | Path, keep: str = "all") -> None:
    """keep: all | copyright | none. GPS and serial numbers are removed for 'copyright'."""
    et = _et()
    if et is None or keep == "all":
        return
    if keep == "none":
        et.execute("-all=", "-P", "-overwrite_original", str(path))
    else:
        et.execute("-gps:all=", "-SerialNumber=", "-InternalSerialNumber=", "-LensSerialNumber=",
                   "-P", "-overwrite_original", str(path))


def copy_metadata(src: str | Path, dst: str | Path, keep: str = "all") -> bool:
    et = _et()
    if et is None:
        return False
    try:
        et.execute("-TagsFromFile", str(src), "-all:all", "-Orientation=1", "-P", "-overwrite_original", str(dst))
        strip_tags_for_export(dst, keep)
        return True
    except Exception:
        return False


def canon_file_number(stem: str) -> int | None:
    m = re.search(r"(\d{4})$", stem)
    return int(m.group(1)) if m else None
