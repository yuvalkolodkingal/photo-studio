"""Self-contained static HTML gallery for an album / selection: responsive CSS grid, vanilla-JS lightbox
(keyboard, swipe, deep links, preloading), text filter, captions from caption / tags / EXIF. No CDN, no external
fonts, no frameworks: the output folder can be opened from disk or dropped on any static host.

    <out>/index.html      page (items are plain <a><img> links first, so it still works without JavaScript)
    <out>/thumbs/NNN_x.jpg
    <out>/images/NNN_x.jpg   web-sized renders (edited images are rendered through the engine by Catalog.thumb)
    <out>/originals/...      only with include_originals=True

GPS never enters the page; tags are shown only when show_tags=True (default) and meta tags such as camera / ISO
are left out of captions.
"""
from __future__ import annotations

import html
import json
import re
import shutil
from pathlib import Path
from typing import Any

META_TAG_ROOTS = {"camera", "lens", "focal", "iso", "date", "time", "season", "orientation", "genre", "ram", "light",
                  "weather", "shot"}


def _caption(im: dict, show_tags: bool) -> dict:
    tags = []
    place = None
    if show_tags:
        for t in im.get("tags", []):
            parts = t.split("|")
            if parts[0].lower() == "places":
                place = parts[-1]
            elif parts[0].lower() not in META_TAG_ROOTS:
                tags.append(parts[-1])
    bits = []
    if im.get("aperture"):
        bits.append(f"f/{im['aperture']:g}")
    if im.get("shutter"):
        s = im["shutter"]
        bits.append(f"1/{round(1 / s)}s" if s < 0.4 else f"{s:g}s")
    if im.get("iso"):
        bits.append(f"ISO {int(im['iso'])}")
    if im.get("focal"):
        bits.append(f"{im['focal']:g}mm")
    return {"title": im.get("caption") or "", "tags": sorted(set(tags)), "place": place,
            "date": (im.get("taken_at") or "")[:16].replace("T", " "), "camera": im.get("camera") or "",
            "exif": "  ".join(bits)}


def build_gallery(cat, source: Any, out_dir: str | Path, title: str | None = None, description: str = "",
                  thumb_size: int = 480, web_size: int = 2048, show_tags: bool = True, include_originals: bool = False,
                  min_thumb: int = 220, images: list[dict] | None = None, show_filenames: bool = False) -> dict:
    """Build the gallery for an album (name / id), a list of image ids, or `images` (catalog rows). Returns a
    report with the index path and counts. Existing files in `out_dir` with the same names are overwritten."""
    from PIL import Image

    if images is None:
        if isinstance(source, (list, tuple)):
            images = [cat._row_dict(cat.conn.execute("SELECT * FROM images WHERE id=?", (i,)).fetchone()) for i in cat._ids(list(source))]
            cat._attach_tags(images)
            title = title or "Gallery"
        else:
            images = cat.albums.images(source)
            alb = cat.albums.get(source)
            title = title or alb["name"]
            description = description or alb.get("description", "")
    if not images:
        raise ValueError("nothing to put in the gallery (empty album / selection)")
    out = Path(out_dir).expanduser()
    for sub in ("thumbs", "images") + (("originals",) if include_originals else ()):
        (out / sub).mkdir(parents=True, exist_ok=True)
    items, errors = [], []
    for n, im in enumerate(images, start=1):
        base = f"{n:03d}_{''.join(c if c.isalnum() or c in '-_' else '_' for c in im['stem'])}"
        try:
            t_src, w_src = cat.thumb(im["id"], thumb_size), cat.thumb(im["id"], web_size)
            shutil.copyfile(t_src, out / "thumbs" / f"{base}.jpg")
            shutil.copyfile(w_src, out / "images" / f"{base}.jpg")
            with Image.open(out / "images" / f"{base}.jpg") as w_im:
                w, h = w_im.size
            with Image.open(out / "thumbs" / f"{base}.jpg") as t_im:
                tw, th = t_im.size
        except Exception as exc:
            errors.append(f"{im['stem']}: {type(exc).__name__}: {exc}")
            continue
        item = {"n": n, "thumb": f"thumbs/{base}.jpg", "src": f"images/{base}.jpg", "w": w, "h": h, "tw": tw, "th": th,
                "name": im["stem"] if show_filenames else "", "rating": im.get("rating", 0), **_caption(im, show_tags)}
        if include_originals:
            o = Path(im["jpg"] or im["path"])
            shutil.copyfile(o, out / "originals" / f"{base}{o.suffix}")
            item["original"] = f"originals/{base}{o.suffix}"
        items.append(item)
    if not items:
        raise RuntimeError("; ".join(errors[:3]) or "no images could be rendered")
    index = out / "index.html"
    index.write_text(render_html(items, title or "Gallery", description, min_thumb), encoding="utf-8")
    return {"index": str(index), "images": len(items), "errors": errors, "out": str(out),
            "bytes": sum(f.stat().st_size for f in out.rglob("*") if f.is_file())}


def render_html(items: list[dict], title: str, description: str = "", min_thumb: int = 220) -> str:
    esc = html.escape
    cards = []
    for it in items:
        alt = it["title"] or ", ".join(it["tags"]) or it["name"] or f"Photo {it['n']}"
        cards.append(
            f'<a class="item" href="{esc(it["src"])}" data-i="{it["n"] - 1}">'
            f'<img src="{esc(it["thumb"])}" width="{it["tw"]}" height="{it["th"]}" loading="lazy" alt="{esc(alt)}"></a>')
    data = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
    desc = f'<p class="desc">{esc(description)}</p>' if description else ""
    subs = {"TITLE": esc(title), "DESC": desc, "CARDS": "\n".join(cards), "DATA": data, "MIN": str(int(min_thumb)),
            "COUNT": str(len(items))}
    return re.sub(r"\{\{([A-Z]+)\}\}", lambda m: subs.get(m.group(1), m.group(0)), _TEMPLATE)


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
:root{--bg:#fafafa;--fg:#1b1b1b;--mut:#666;--card:#e8e8e8;--ov:rgba(0,0,0,.94);--min:{{MIN}}px;--gap:6px}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#ececec;--mut:#9a9a9a;--card:#1f1f1f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:1.2rem 1rem .4rem;max-width:1600px;margin:0 auto}
h1{margin:0;font-size:1.6rem;font-weight:600}
.desc{margin:.3rem 0 0;color:var(--mut)}
.bar{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin-top:.8rem;color:var(--mut);font-size:.9rem}
input[type=search]{padding:.4rem .6rem;border:1px solid var(--card);border-radius:6px;background:var(--bg);color:var(--fg);min-width:12rem}
main{max-width:1600px;margin:0 auto;padding:.5rem 1rem 3rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--min),1fr));gap:var(--gap)}
.item{display:block;background:var(--card);aspect-ratio:3/2;overflow:hidden;border-radius:3px;outline-offset:2px}
.item img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .25s}
.item:hover img,.item:focus img{transform:scale(1.04)}
.item[hidden]{display:none}
#lb{position:fixed;inset:0;background:var(--ov);display:none;flex-direction:column;z-index:10;color:#eee;touch-action:pan-y}
#lb.open{display:flex}
#lb .stage{flex:1;display:flex;align-items:center;justify-content:center;min-height:0;position:relative}
#lb img{max-width:100%;max-height:100%;object-fit:contain;user-select:none;-webkit-user-drag:none}
#lb.zoom .stage{overflow:auto;align-items:flex-start;justify-content:flex-start}
#lb.zoom img{max-width:none;max-height:none;cursor:zoom-out}
#lb .cap{padding:.6rem 1rem .9rem;text-align:center;font-size:.9rem;max-height:28vh;overflow:auto}
#lb .cap b{font-weight:600}.cap small{color:#aaa;display:block}
#lb button{position:absolute;background:rgba(255,255,255,.08);border:0;color:#fff;font-size:1.6rem;width:3rem;height:3rem;border-radius:50%;cursor:pointer;z-index:2}
#lb button:hover,#lb button:focus{background:rgba(255,255,255,.22)}
#lb .prev{left:.6rem;top:50%;margin-top:-1.5rem}#lb .next{right:.6rem;top:50%;margin-top:-1.5rem}
#lb .close{right:.6rem;top:.6rem}
#lb .count{position:absolute;left:.8rem;top:.8rem;font-size:.85rem;color:#bbb}
@media (max-width:600px){:root{--min:150px;--gap:3px}#lb .prev,#lb .next{display:none}}
</style>
</head>
<body>
<header>
<h1>{{TITLE}}</h1>
{{DESC}}
<div class="bar"><span id="n">{{COUNT}} photos</span><input id="q" type="search" placeholder="Filter by tag, place, caption" aria-label="Filter photos"></div>
</header>
<main><div class="grid" id="grid">
{{CARDS}}
</div></main>
<div id="lb" role="dialog" aria-modal="true" aria-label="Photo viewer">
<span class="count" id="cnt"></span>
<button class="close" aria-label="Close">&times;</button>
<div class="stage"><button class="prev" aria-label="Previous">&#8249;</button><img id="big" alt=""><button class="next" aria-label="Next">&#8250;</button></div>
<div class="cap" id="cap"></div>
</div>
<script type="application/json" id="data">{{DATA}}</script>
<script>
(function(){
var D=JSON.parse(document.getElementById('data').textContent);
var lb=document.getElementById('lb'),big=document.getElementById('big'),cap=document.getElementById('cap'),cnt=document.getElementById('cnt');
var grid=document.getElementById('grid'),items=[].slice.call(grid.children),cur=-1,vis=[];
function visible(){return items.map(function(a,i){return a.hidden?-1:i}).filter(function(i){return i>=0})}
function el(t,txt,cls){var e=document.createElement(t);if(cls)e.className=cls;e.textContent=txt;return e}
function show(i){
  var d=D[i];if(!d)return;cur=i;vis=visible();
  lb.classList.remove('zoom');big.src=d.src;big.alt=d.title||d.tags.join(', ');
  cap.textContent='';
  var head=[d.title,d.name].filter(Boolean).join(' - ');if(head)cap.appendChild(el('b',head));
  var line=[d.place,d.tags.join(', ')].filter(Boolean).join('  |  ');if(line)cap.appendChild(el('div',line));
  var meta=[d.date,d.camera,d.exif].filter(Boolean).join('  ·  ');if(meta)cap.appendChild(el('small',meta));
  if(d.original){var a=el('a','original');a.href=d.original;a.style.color='#9cf';a.download='';var s=el('small','');s.appendChild(a);cap.appendChild(s)}
  cnt.textContent=(vis.indexOf(i)+1)+' / '+vis.length;
  lb.classList.add('open');document.body.style.overflow='hidden';
  if(location.hash!=='#'+(i+1))history.replaceState(null,'','#'+(i+1));
  [1,-1].forEach(function(k){var j=vis[vis.indexOf(i)+k];if(j!==undefined){new Image().src=D[j].src}});
}
function close(){lb.classList.remove('open');document.body.style.overflow='';cur=-1;history.replaceState(null,'',location.pathname+location.search)}
function step(k){if(cur<0)return;vis=visible();var p=vis.indexOf(cur)+k;if(p<0)p=vis.length-1;if(p>=vis.length)p=0;show(vis[p])}
grid.addEventListener('click',function(e){var a=e.target.closest('a.item');if(!a)return;e.preventDefault();show(+a.dataset.i)});
lb.querySelector('.close').onclick=close;lb.querySelector('.prev').onclick=function(){step(-1)};lb.querySelector('.next').onclick=function(){step(1)};
lb.addEventListener('click',function(e){if(e.target===lb||e.target.classList.contains('stage'))close()});
big.addEventListener('click',function(){lb.classList.toggle('zoom')});
document.addEventListener('keydown',function(e){if(cur<0)return;if(e.key==='Escape')close();else if(e.key==='ArrowRight')step(1);else if(e.key==='ArrowLeft')step(-1)});
var sx=null;lb.addEventListener('touchstart',function(e){sx=e.touches[0].clientX},{passive:true});
lb.addEventListener('touchend',function(e){if(sx===null)return;var dx=e.changedTouches[0].clientX-sx;sx=null;if(Math.abs(dx)>50)step(dx<0?1:-1)});
var q=document.getElementById('q'),n=document.getElementById('n');
q.addEventListener('input',function(){var s=q.value.trim().toLowerCase();var c=0;
  items.forEach(function(a,i){var d=D[i];var hay=[d.title,d.name,d.place,d.tags.join(' '),d.camera,d.date].join(' ').toLowerCase();var ok=!s||s.split(/\\s+/).every(function(w){return hay.indexOf(w)>=0});a.hidden=!ok;if(ok)c++});
  n.textContent=c+(c===1?' photo':' photos')});
var m=location.hash.match(/^#(\\d+)$/);if(m&&D[+m[1]-1])show(+m[1]-1);
})();
</script>
</body>
</html>
"""
