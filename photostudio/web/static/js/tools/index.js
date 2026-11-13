import { session } from '../session.js';
import { bus } from '../store.js';
import { cropTool, levelTool } from './crop.js';
import { wbTool } from './wb.js';
import { healTool } from './heal.js';
import { maskTool } from './mask.js';

/** One tool at a time on the viewer. `active` is the tool name or null. */
export function createTools(viewer) {
  const T = { viewer, active: null, cropTool, levelTool, wbTool, healTool, maskTool };
  const names = { crop: cropTool, level: levelTool, wb: wbTool, heal: healTool, mask: maskTool };
  bus.on('tool', (n) => { T.active = n; });
  function use(name) {
    if (name && T.active === name && name !== 'mask') { T.none(); return; }
    viewer.setTool(names[name] || null);
    T.active = name;
  }
  T.crop = () => use('crop');
  T.cropOn = () => { if (T.active !== 'crop') use('crop'); };
  T.level = () => use('level');
  T.wbPicker = () => use('wb');
  T.heal = () => use('heal');
  T.mask = () => { if (T.active !== 'mask') use('mask'); };
  T.none = () => { if (T.active) { viewer.setTool(null); T.active = null; bus.emit('tool', null); } };
  T.key = (e) => (viewer.tool && viewer.tool.key ? viewer.tool.key(e) : false);
  bus.on('tool:done', () => T.none());
  bus.on('tool:cancel', () => T.none());
  bus.on('dev:loaded', () => { if (T.active && T.active !== 'mask') T.none(); });
  return T;
}
export { session };
