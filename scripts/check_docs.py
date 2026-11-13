#!/usr/bin/env python3
"""Static checks for the skill documentation.

* SKILL.md frontmatter parses, name is photo-studio, description <= 1024 chars, body < 500 lines
* every relative markdown link (and #anchor) in SKILL.md, README.md and references/*.md resolves
* reference files over 300 lines have a Contents section
* every ``photo ...`` command in a ```bash fence uses a real subcommand and real flags (checked against ``--help``)
* evals/evals.json is well formed

Run: .venv/bin/python scripts/check_docs.py        (exit code 1 if anything fails)
It does not execute the examples; see the sandbox recipe in references/troubleshooting.md for that.
"""
from __future__ import annotations

import functools
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PHOTO = str(ROOT / ".venv" / "bin" / "photo")
if not Path(PHOTO).exists():
    PHOTO = shutil.which("photo") or PHOTO
errs: list[str] = []


def check_skill() -> None:
    s = (ROOT / "SKILL.md").read_text()
    m = re.match(r"---\n(.*?)\n---\n", s, re.S)
    if not m:
        errs.append("SKILL.md: no frontmatter")
        return
    fm = yaml.safe_load(m.group(1))
    if fm.get("name") != "photo-studio":
        errs.append("SKILL.md: name must be photo-studio")
    if not 50 <= len(fm.get("description", "")) <= 1024:
        errs.append(f"SKILL.md: description length {len(fm.get('description', ''))}")
    if s.count("\n") >= 500:
        errs.append(f"SKILL.md: {s.count(chr(10))} lines (limit 500)")
    print(f"SKILL.md: {s.count(chr(10))} lines, description {len(fm['description'])} chars")


def slug(h: str) -> str:
    h = re.sub(r"[`*]", "", h.strip().lower())
    return re.sub(r"[^\w\- ]", "", h).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    out, fence = set(), False
    for ln in path.read_text().splitlines():
        if ln.startswith("```"):
            fence = not fence
        if not fence and re.match(r"#{1,6} ", ln):
            out.add(slug(ln.lstrip("#")))
    return out


def docs() -> list[Path]:
    return [ROOT / "SKILL.md", ROOT / "README.md"] + sorted((ROOT / "references").glob("*.md"))


def check_links() -> None:
    for md in docs():
        for lm in re.finditer(r"\[[^\]]*\]\(([^)\s]+)\)", md.read_text()):
            tgt = lm.group(1)
            if tgt.startswith(("http", "mailto")):
                continue
            p, _, a = tgt.partition("#")
            f = (md.parent / p) if p else md
            if not f.exists():
                errs.append(f"{md.name}: missing link target {tgt}")
            elif a and f.suffix == ".md" and a not in anchors(f):
                errs.append(f"{md.name}: bad anchor {tgt}")
    for md in sorted((ROOT / "references").glob("*.md")):
        text = md.read_text()
        if text.count("\n") > 300 and "## Contents" not in text:
            errs.append(f"{md.name}: over 300 lines without a Contents section")


@functools.lru_cache(None)
def helptext(args: tuple) -> tuple[int, str]:
    r = subprocess.run([PHOTO, *args, "--help"], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def flags_of(txt: str) -> set[str]:
    return set(re.findall(r"(?<![\w-])(--?[A-Za-z][\w-]*)", txt))


def commands(md: Path) -> list[str]:
    out, fence, cur = [], None, None
    for ln in md.read_text().splitlines():
        if ln.startswith("```"):
            fence = None if fence else ln[3:].strip()
            continue
        if fence not in ("bash", "sh", "shell"):
            continue
        if cur is not None:
            cur += " " + ln.strip().rstrip("\\ ")
            if not ln.rstrip().endswith("\\"):
                out.append(cur)
                cur = None
            continue
        if re.search(r"(^|[|&;]\s*|xargs -d '\\n' )photo ", ln.strip()):
            if ln.rstrip().endswith("\\"):
                cur = ln.strip().rstrip("\\ ")
            else:
                out.append(ln.strip())
    return out


def split_commands(line: str) -> list[list[str]]:
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = "#"
    toks = list(lex)
    cmds, cur = [], []
    for t in toks:
        if set(t) <= set("&|;()"):
            cmds.append(cur)
            cur = []
        else:
            cur.append(t)
    cmds.append(cur)
    return [c for c in cmds if c and c[0] == "photo"]


def check_commands() -> None:
    checked = 0
    for md in docs():
        for line in commands(md):
            try:
                parts = split_commands(line)
            except ValueError:
                errs.append(f"{md.name}: cannot parse {line[:70]}")
                continue
            for toks in parts:
                rest = toks[1:]
                i = 0
                while i < len(rest) and rest[i].startswith("-"):
                    i += 2 if rest[i] == "--root" else 1
                rest = rest[i:]
                if not rest or rest[0].isupper():
                    continue  # placeholder such as `photo COMMAND`
                sub = rest[0]
                rc, h = helptext((sub,))
                if rc != 0:
                    errs.append(f"{md.name}: unknown subcommand '{sub}' in: {line[:70]}")
                    continue
                args = (sub,)
                for depth in (1, 2):
                    if len(rest) > depth and not rest[depth].startswith("-"):
                        rc2, h2 = helptext(args + (rest[depth],))
                        if rc2 == 0 and h2.startswith("usage: photo " + " ".join(args + (rest[depth],))):
                            args, h = args + (rest[depth],), h2
                known = flags_of(h) | {"-h", "--help", "--json", "--quiet", "--root", "--debug", "-q"}
                for t in rest:
                    if t.startswith("--") or (t.startswith("-") and len(t) == 2 and t[1].isalpha()):
                        fl = t.split("=")[0]
                        if fl not in known:
                            errs.append(f"{md.name}: `photo {' '.join(args)}` has no {fl}: {line[:80]}")
                checked += 1
    print(f"commands checked against --help: {checked}")


def check_evals() -> None:
    d = json.loads((ROOT / "evals" / "evals.json").read_text())
    assert d["skill_name"] == "photo-studio"
    for e in d["evals"]:
        for k in ("id", "prompt", "expected_output", "files"):
            if k not in e:
                errs.append(f"evals.json: eval {e.get('id')} lacks {k}")
    print(f"evals: {len(d['evals'])}")


def main() -> int:
    check_skill()
    check_links()
    check_commands()
    check_evals()
    for e in errs:
        print("ERROR:", e)
    print("FAILED" if errs else "OK")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
