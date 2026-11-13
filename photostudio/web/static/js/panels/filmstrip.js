import { h } from '../util.js';
import { session } from '../session.js';
import * as api from '../api.js';
import { state, bus } from '../store.js';

export function build({ open }) {
  const el = h('div.filmstrip');
  let cells = new Map();
  function render() {
    cells = new Map();
    el.replaceChildren(...state.items.map((it) => {
      const img = h('img', { loading: 'lazy', alt: '', draggable: 'false' });
      img.src = api.thumbUrl(it.p, 160, it.ops_hash || '');
      const c = h('div.fc', { title: it.name, class: it.p === session.p ? 'cur' : '', onclick: () => open(it.p) }, img, h('span.nm', { text: it.name }), it.has_edits ? h('span.ed', { title: 'Edited' }) : null);
      cells.set(it.p, c);
      return c;
    }));
    mark();
  }
  function mark() {
    for (const [p, c] of cells) c.classList.toggle('cur', p === session.p);
    const c = cells.get(session.p); if (c) c.scrollIntoView({ block: 'nearest', inline: 'center' });
  }
  bus.on('lib:listed', render);
  bus.on('dev:loaded', mark);
  bus.on('lib:item', (p) => {
    const it = state.itemsByPath.get(p), c = cells.get(p);
    if (!it || !c) return;
    const img = c.querySelector('img'); if (img) img.src = api.thumbUrl(p, 160, it.ops_hash || String(Date.now()));
    if (it.has_edits && !c.querySelector('.ed')) c.append(h('span.ed'));
  });
  return { el, render, mark };
}
