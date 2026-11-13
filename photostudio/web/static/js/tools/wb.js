import { session } from '../session.js';
import * as S from '../stack.js';
import * as api from '../api.js';
import { bus } from '../store.js';
import { toastError } from '../toast.js';

/** White-balance eyedropper: click a neutral area of the un-cropped picture; the server solves temperature / tint. */
export const wbTool = {
  name: 'wb', cursor: 'crosshair', noDblZoom: true,
  hint: 'Click a neutral grey or white area.',
  activate() { session.setNogeo(true); bus.emit('tool', 'wb'); bus.emit('tool:hint', this.hint); },
  deactivate() { session.setNogeo(false); bus.emit('tool', null); bus.emit('tool:hint', ''); },
  pointerdown(pt) {
    if (pt.nx < 0 || pt.nx > 1 || pt.ny < 0 || pt.ny > 1) return false;
    api.wbPick(session.p, pt.nx, pt.ny).then((r) => {
      session.edit('White balance picker', (st) => S.setParams(st, 'white_balance', { temperature: Math.round(r.temperature), tint: Math.round(r.tint * 10) / 10, auto: null, picker: null }));
      bus.emit('tool:done');
    }).catch(toastError);
    return true;
  },
  key(e) { if (e.key === 'Escape') { bus.emit('tool:cancel'); return true; } return false; },
};
