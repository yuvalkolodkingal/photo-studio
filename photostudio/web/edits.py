"""Sidecar editing service: load/save stacks (read-modify-write so rating/flag/tags written by the CLI survive),
per-image undo/redo history, versions, snapshots, copy/paste/sync, plus the external-change watcher."""
from __future__ import annotations

import collections
import copy
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .. import sidecar
from ..pipeline import engine
from ..presets import normalise_entry
from ..util import params_hash
from . import paths as P

log = logging.getLogger("photostudio.web")

ENTRY_KEYS = ("op", "params", "enabled", "mask", "blend", "opacity", "v", "id", "label")
MAX_HISTORY = 200


class EditError(ValueError):
    pass


class Conflict(Exception):
    """The sidecar's stack was changed on disk by someone else since this server last read/wrote it."""


def clean_stack(stack: Any) -> list[dict]:
    """Validate/normalise a client supplied stack (list of entries); strips unknown keys."""
    if not isinstance(stack, list):
        raise EditError("stack must be a list of entries")
    out = []
    for i, e in enumerate(stack):
        if not isinstance(e, dict) or not isinstance(e.get("op"), str):
            raise EditError(f"stack[{i}]: entry needs a string 'op'")
        e = {k: e[k] for k in ENTRY_KEYS if k in e}
        if e.get("params") is not None and not isinstance(e["params"], dict):
            raise EditError(f"stack[{i}] ({e['op']}): params must be an object")
        if e.get("mask") is not None and not (isinstance(e["mask"], dict) and "type" in e["mask"]):
            raise EditError(f"stack[{i}] ({e['op']}): mask needs a 'type'")
        try:
            op_ = float(e.get("opacity", 1.0))
        except (TypeError, ValueError) as exc:
            raise EditError(f"stack[{i}]: opacity must be a number") from exc
        e["opacity"] = min(1.0, max(0.0, op_))
        out.append(normalise_entry(e))
    return out


def stack_hash(stack: list[dict]) -> str:
    return params_hash([{k: e.get(k) for k in ENTRY_KEYS} for e in stack])


class History:
    """Linear undo/redo of one (image, version) stack. Rapid edits with the same label coalesce."""

    def __init__(self):
        self.entries: list[dict] = []
        self.index = -1
        self._n = 0

    def push(self, stack: list[dict], label: str, coalesce_s: float = 2.0) -> bool:
        h = stack_hash(stack)
        if self.entries and self.entries[self.index]["hash"] == h:
            return False
        now = time.time()
        del self.entries[self.index + 1:]
        last = self.entries[-1] if self.entries else None
        if last and len(self.entries) > 1 and last["label"] == label and now - last["time"] < coalesce_s:
            last.update(stack=copy.deepcopy(stack), hash=h, time=now)
        else:
            self._n += 1
            self.entries.append({"id": self._n, "label": label, "time": now, "stack": copy.deepcopy(stack), "hash": h})
            if len(self.entries) > MAX_HISTORY:
                del self.entries[0]
        self.index = len(self.entries) - 1
        return True

    def reset(self, stack: list[dict], label: str = "Open") -> None:
        self.entries, self.index = [], -1
        self.push(stack, label)

    def undo(self) -> dict | None:
        if self.index <= 0:
            return None
        self.index -= 1
        return self.entries[self.index]

    def redo(self) -> dict | None:
        if self.index >= len(self.entries) - 1:
            return None
        self.index += 1
        return self.entries[self.index]

    def goto(self, entry_id: int) -> dict | None:
        for i, e in enumerate(self.entries):
            if e["id"] == entry_id:
                self.index = i
                return e
        return None

    def listing(self) -> dict:
        return {"index": self.index, "can_undo": self.index > 0, "can_redo": self.index < len(self.entries) - 1,
                "entries": [{"id": e["id"], "label": e["label"], "time": e["time"], "n_ops": len(e["stack"])} for e in self.entries]}


class EditService:
    def __init__(self, root: Path, events: "EventLog", on_saved=None):
        self.root = root
        self.events = events
        self.on_saved = on_saved
        self._locks: dict[str, threading.RLock] = collections.defaultdict(threading.RLock)
        self._guard = threading.Lock()
        self.history: dict[tuple[str, str], History] = {}
        self.known: dict[tuple[str, str], str] = {}   # (rel, version) -> stack hash last read/written by us
        self.opened_at: dict[str, float] = {}
        self._meta_cache: dict[tuple, dict] = {}

    # ------------------------------------------------------------------ helpers
    def lock(self, rel: str) -> threading.RLock:
        with self._guard:
            return self._locks[rel]

    def _path(self, rel: str) -> Path:
        return P.resolve_image(self.root, rel)

    def metadata(self, path: Path) -> dict:
        try:
            st = path.stat()
        except OSError:
            return {}
        key = (str(path), st.st_mtime_ns)
        if key not in self._meta_cache:
            try:
                from ..metadata import read_metadata

                md = read_metadata(path)
            except Exception as exc:  # noqa: BLE001
                log.debug("metadata failed for %s: %s", path, exc)
                md = {}
            if len(self._meta_cache) > 2000:
                self._meta_cache.clear()
            self._meta_cache[key] = md
        return self._meta_cache[key]

    def sidecar_mtime(self, path: Path) -> int:
        try:
            return sidecar.sidecar_path(path).stat().st_mtime_ns
        except OSError:
            return 0

    def _hist(self, rel: str, version: str) -> History:
        return self.history.setdefault((rel, version), History())

    def effective_stack(self, path: Path, doc: dict, version: str) -> tuple[list[dict], bool]:
        """(stack, materialised): the stored stack, or the resolved defaults if the version is empty."""
        stack = sidecar.get_stack(doc, version)
        if stack:
            return stack, True
        v = doc["versions"][version]
        meta = self.metadata(path)
        return engine.resolve_stack(v.get("mode"), v.get("art"), None, engine.source_kind(path), meta), False

    # ------------------------------------------------------------------ load
    def load(self, rel: str, version: str | None = None) -> dict:
        path = self._path(rel)
        rel = P.to_rel(self.root, path)
        with self.lock(rel):
            doc = sidecar.load(path)
            vname = version or doc["active"]
            if vname not in doc["versions"]:
                raise EditError(f"no version '{vname}'")
            stack, mat = self.effective_stack(path, doc, vname)
            h = stack_hash(stack)
            key = (rel, vname)
            hist = self._hist(rel, vname)
            on_disk = stack_hash(sidecar.get_stack(doc, vname)) if mat else "empty"
            if not hist.entries or (self.known.get(key) is not None and self.known[key] != on_disk):
                hist.reset(stack, "Open")
            self.known[key] = on_disk
            self.opened_at[rel] = time.time()
            return self._payload(path, rel, doc, vname, stack, mat)

    def _payload(self, path: Path, rel: str, doc: dict, vname: str, stack: list[dict], materialised: bool) -> dict:
        v = doc["versions"][vname]
        ops_h = sidecar.ops_hash(stack) if materialised else ""
        return {
            "p": rel, "name": path.name, "kind": engine.source_kind(path), "version": vname, "active": doc["active"],
            "stack": stack, "materialised": materialised, "mode": v.get("mode"), "art": v.get("art"),
            "versions": sidecar.list_versions(doc), "snapshots": sidecar.list_snapshots(doc),
            "rating": doc.get("rating", 0), "flag": doc.get("flag"), "label": doc.get("label"), "tags": doc.get("tags", []),
            "notes": doc.get("notes", ""), "mtime_ns": self.sidecar_mtime(path), "ops_hash": ops_h,
            "modified": v.get("modified"), "history": self._hist(rel, vname).listing(),
            "default_ops": self._default_ops(path),
        }

    def _default_ops(self, path: Path) -> list[str]:
        """Ops of the engine's default stack (the UI keeps these entries on reset instead of deleting them)."""
        try:
            return [e["op"] for e in engine.resolve_stack(None, None, None, engine.source_kind(path), self.metadata(path))]
        except Exception:  # noqa: BLE001
            return ["exposure", "white_balance", "tone_map"]

    # ------------------------------------------------------------------ save
    def save(self, rel: str, stack: list[dict], version: str | None = None, label: str = "Edit", mode: Any = ..., art: Any = ...,
             force: bool = False, snapshot_first: bool = False, push_history: bool = True) -> dict:
        path = self._path(rel)
        rel = P.to_rel(self.root, path)
        stack = clean_stack(stack)
        with self.lock(rel):
            doc = sidecar.load(path)
            vname = version or doc["active"]
            if vname not in doc["versions"]:
                raise EditError(f"no version '{vname}'")
            key = (rel, vname)
            disk_stack = sidecar.get_stack(doc, vname)
            disk_h = stack_hash(disk_stack) if disk_stack else "empty"
            known = self.known.get(key)
            if not force and known is not None and disk_h != known and disk_h != "empty" and disk_h != stack_hash(stack):
                raise Conflict(f"{rel} was edited elsewhere (CLI / Claude)")
            if snapshot_first and disk_stack:
                sidecar.snapshot(doc, "auto:web-session", vname)
            kw = {}
            if mode is not ...:
                kw["mode"] = mode
            if art is not ...:
                kw["art"] = art
            sidecar.set_stack(doc, stack, vname, **kw)
            sp = sidecar.save(path, doc)
            self.known[key] = stack_hash(stack)
            mt = sp.stat().st_mtime_ns
            self.events.note_own_write(rel, mt)
            if push_history:
                self._hist(rel, vname).push(stack, label)
            payload = {"p": rel, "mtime_ns": mt, "ops_hash": sidecar.ops_hash(stack), "modified": doc["versions"][vname]["modified"],
                       "history": self._hist(rel, vname).listing(), "version": vname}
        if self.on_saved:
            self.on_saved(rel)
        return payload

    # ------------------------------------------------------------------ undo / redo
    def _apply_history(self, rel: str, entry: dict | None, version: str | None) -> dict:
        path = self._path(rel)
        rel = P.to_rel(self.root, path)
        with self.lock(rel):
            doc = sidecar.load(path)
            vname = version or doc["active"]
            hist = self._hist(rel, vname)
            if entry is None:
                stack, mat = self.effective_stack(path, doc, vname)
                return {**self._payload(path, rel, doc, vname, stack, mat), "changed": False}
            res = self.save(rel, entry["stack"], vname, label=entry["label"], force=True, push_history=False)
            doc = sidecar.load(path)
            stack, mat = self.effective_stack(path, doc, vname)
            out = self._payload(path, rel, doc, vname, stack, mat)
            out["changed"] = True
            out["mtime_ns"] = res["mtime_ns"]
            out["history"] = hist.listing()
            return out

    def undo(self, rel: str, version: str | None = None) -> dict:
        path = self._path(rel)
        rel = P.to_rel(self.root, path)
        vname = version or sidecar.load(path)["active"]
        return self._apply_history(rel, self._hist(rel, vname).undo(), vname)

    def redo(self, rel: str, version: str | None = None) -> dict:
        path = self._path(rel)
        rel = P.to_rel(self.root, path)
        vname = version or sidecar.load(path)["active"]
        return self._apply_history(rel, self._hist(rel, vname).redo(), vname)

    def goto(self, rel: str, entry_id: int, version: str | None = None) -> dict:
        path = self._path(rel)
        rel = P.to_rel(self.root, path)
        vname = version or sidecar.load(path)["active"]
        e = self._hist(rel, vname).goto(int(entry_id))
        if e is None:
            raise EditError("no such history entry")
        return self._apply_history(rel, e, vname)

    # ------------------------------------------------------------------ versions / snapshots / reset
    def _mutate(self, rel: str, fn, version_hint: str | None = None) -> dict:
        path = self._path(rel)
        rel = P.to_rel(self.root, path)
        with self.lock(rel):
            doc = sidecar.load(path)
            try:
                fn(doc)
            except sidecar.SidecarError as exc:
                raise EditError(str(exc)) from exc
            sp = sidecar.save(path, doc)
            self.events.note_own_write(rel, sp.stat().st_mtime_ns)
            vname = doc["active"]
            stack, mat = self.effective_stack(path, doc, vname)
            self.known[(rel, vname)] = stack_hash(sidecar.get_stack(doc, vname)) if mat else "empty"
            hist = self._hist(rel, vname)
            if not hist.entries or hist.entries[hist.index]["hash"] != stack_hash(stack):
                hist.push(stack, "Switch version" if version_hint == "switch" else "Edit")
            out = self._payload(path, rel, doc, vname, stack, mat)
        if self.on_saved:
            self.on_saved(rel)
        return out

    def versions(self, rel: str, action: str, name: str | None = None, new_name: str | None = None,
                 from_version: str | None = None) -> dict:
        def fn(doc):
            if action == "create":
                if not name:
                    raise EditError("name required")
                sidecar.new_version(doc, name, from_version=from_version, switch=True)
            elif action == "switch":
                sidecar.switch_version(doc, name or "")
            elif action == "rename":
                if not name or not new_name:
                    raise EditError("name and new_name required")
                sidecar.rename_version(doc, name, new_name)
            elif action == "delete":
                sidecar.delete_version(doc, name or "")
            else:
                raise EditError(f"unknown action '{action}'")

        out = self._mutate(rel, fn, "switch" if action in ("switch", "create", "delete") else None)
        if action in ("rename", "delete"):
            self.history = {k: v for k, v in self.history.items() if not (k[0] == out["p"] and k[1] == name)}
        return out

    def snapshot(self, rel: str, action: str, name: str, version: str | None = None) -> dict:
        def fn(doc):
            if action == "create":
                if not name:
                    raise EditError("snapshot name required")
                sidecar.snapshot(doc, name, version)
            elif action == "restore":
                sidecar.restore_snapshot(doc, name, version)
            elif action == "delete":
                snaps = doc["snapshots"]
                idx = [i for i, s in enumerate(snaps) if s.get("name") == name]
                if not idx:
                    raise EditError(f"no snapshot '{name}'")
                del snaps[idx[-1]]
            else:
                raise EditError(f"unknown action '{action}'")

        return self._mutate(rel, fn)

    def reset(self, rel: str, version: str | None = None) -> dict:
        return self._mutate(rel, lambda doc: sidecar.reset(doc, version))

    # ------------------------------------------------------------------ copy / paste / sync
    def copy_settings(self, rel: str, ops: list[str] | None = None, version: str | None = None) -> dict:
        path = self._path(rel)
        clip = sidecar.copy_settings(path, ops or None, version)
        return {"source": clip["source"], "n_ops": len(clip["stack"]), "ops": [e["op"] for e in clip["stack"]]}

    def paste_settings(self, rels: list[str], mode: str = "replace", ops: list[str] | None = None,
                       version: str | None = None) -> dict:
        done = []
        for r in rels:
            path = self._path(r)
            rel = P.to_rel(self.root, path)
            with self.lock(rel):
                try:
                    doc = sidecar.paste_settings(path, version, mode, ops or None)
                except sidecar.SidecarError as exc:
                    raise EditError(str(exc)) from exc
                self._after_external_write(rel, path, doc)
            done.append(rel)
        return {"pasted": done}

    def sync_settings(self, src: str, dsts: list[str], ops: list[str] | None = None, mode: str = "replace",
                      version: str | None = None) -> dict:
        spath = self._path(src)
        dpaths = [self._path(d) for d in dsts]
        try:
            sidecar.copy_between(spath, dpaths, ops or None, version, mode)
        except sidecar.SidecarError as exc:
            raise EditError(str(exc)) from exc
        for d in dpaths:
            rel = P.to_rel(self.root, d)
            self._after_external_write(rel, d, sidecar.load(d))
        return {"synced": [P.to_rel(self.root, d) for d in dpaths]}

    def _after_external_write(self, rel: str, path: Path, doc: dict) -> None:
        mt = self.sidecar_mtime(path)
        self.events.note_own_write(rel, mt)
        vname = doc["active"]
        self.known[(rel, vname)] = stack_hash(sidecar.get_stack(doc, vname))
        self.history.pop((rel, vname), None)
        self.events.emit({"type": "sidecar", "p": rel, "mtime_ns": mt, "external": False})
        if self.on_saved:
            self.on_saved(rel)


class EventLog:
    """Sequenced event log + watcher for sidecars changed behind our back (Claude editing through the CLI)."""

    def __init__(self):
        self._cv = threading.Condition()
        self._seq = 0
        self._events: collections.deque = collections.deque(maxlen=1000)
        self._own: dict[str, int] = {}
        self._mt: dict[str, int] = {}
        self._watch: "collections.OrderedDict[str, float]" = collections.OrderedDict()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.root: Path | None = None

    def emit(self, ev: dict) -> int:
        with self._cv:
            self._seq += 1
            ev = {"seq": self._seq, "time": time.time(), **ev}
            self._events.append(ev)
            self._cv.notify_all()
            return self._seq

    def note_own_write(self, rel: str, mtime_ns: int) -> None:
        self._own[rel] = mtime_ns
        self._mt[rel] = mtime_ns

    def watch(self, rels: list[str], replace: bool = False) -> None:
        if replace:
            self._watch.clear()
        for r in rels[:2000]:
            self._watch[r] = time.time()
            self._watch.move_to_end(r)
            if r not in self._mt and self.root is not None:
                self._mt[r] = self._stat(r)
        while len(self._watch) > 2000:
            self._watch.popitem(last=False)

    def _stat(self, rel: str) -> int:
        try:
            return (self.root / (rel + sidecar.SUFFIX)).stat().st_mtime_ns
        except OSError:
            return 0

    def poll_once(self) -> list[dict]:
        out = []
        for rel in list(self._watch):
            mt = self._stat(rel)
            if mt != self._mt.get(rel, 0):
                self._mt[rel] = mt
                if self._own.get(rel) == mt:
                    continue
                ev = {"type": "sidecar", "p": rel, "mtime_ns": mt, "external": True}
                self.emit(ev)
                out.append(ev)
        return out

    def since(self, seq: int, wait: float = 0.0) -> dict:
        with self._cv:
            if wait > 0 and not any(e["seq"] > seq for e in self._events):
                self._cv.wait(timeout=wait)
            evs = [e for e in self._events if e["seq"] > seq]
            return {"seq": self._seq, "events": evs}

    def start(self, root: Path, interval: float = 1.0) -> None:
        self.root = root
        if self._thread and self._thread.is_alive():
            return

        def run():
            while not self._stop.wait(interval):
                try:
                    self.poll_once()
                except Exception as exc:  # noqa: BLE001
                    log.debug("watcher error: %s", exc)

        self._thread = threading.Thread(target=run, name="ps-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
