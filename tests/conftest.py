"""Shared fixtures: isolated state dirs, a tiny library of synthetic JPEGs and fake raws.

Fake raws are ``.npz`` files built by ``photostudio.testing.make_fake_raw``. The ``cr3_lib`` fixture renames one to
``.CR3`` and patches ``decode._rawpy_imread`` so the real raw code path (extension dispatch) is exercised without rawpy.
"""
from __future__ import annotations

import os
import shutil
import warnings
from pathlib import Path

import numpy as np
import pytest

warnings.filterwarnings("ignore", message=".*Matplotlib.*")
warnings.filterwarnings("ignore", category=UserWarning, module="colour")

from photostudio import testing  # noqa: E402

W, H = 480, 320


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Keep sidecar clipboard, caches and user presets out of the real home directory."""
    home = tmp_path / "_state"
    monkeypatch.setenv("PHOTOSTUDIO_HOME", str(home / "data"))
    monkeypatch.setenv("PHOTOSTUDIO_CACHE", str(home / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.delenv("PHOTOSTUDIO_PRESETS", raising=False)
    from photostudio.pipeline import engine

    engine.clear_caches()
    yield
    engine.clear_caches()


@pytest.fixture(scope="session")
def scene() -> np.ndarray:
    return testing.make_scene(W, H, seed=3)


@pytest.fixture()
def lib(tmp_path, scene) -> Path:
    """A library directory with two JPEGs (one with EXIF) and one fake raw (.npz)."""
    d = tmp_path / "lib"
    d.mkdir()
    testing.write_jpeg(d / "a.jpg", scene, exif={"Make": "Canon", "Model": "Canon EOS R50", "ISOSpeedRatings": 200,
                                                 "FNumber": 8.0, "FocalLength": 35.0,
                                                 "DateTimeOriginal": "2026:11:12 10:00:00"})
    testing.write_jpeg(d / "b.jpg", np.ascontiguousarray(scene[::-1]))
    testing.make_fake_raw(d / "raw1.npz", scene)
    return d


@pytest.fixture()
def jpeg(lib) -> Path:
    return lib / "a.jpg"


@pytest.fixture()
def raw(lib) -> Path:
    return lib / "raw1.npz"


@pytest.fixture()
def cr3_lib(tmp_path, scene, monkeypatch) -> Path:
    """Directory with ``shot.CR3`` (a renamed fake raw) and the rawpy opener patched to read it."""
    from photostudio import decode

    d = tmp_path / "cr3"
    d.mkdir()
    tmp_npz = testing.make_fake_raw(d / "tmp.npz", scene, meta={"ISO": 800})
    shutil.move(tmp_npz, d / "shot.CR3")
    monkeypatch.setattr(decode, "_rawpy_imread", testing.FakeRawPy.load)
    return d


def has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def sample_env() -> dict:
    return dict(os.environ)
