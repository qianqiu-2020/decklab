'use strict';
// Pinned xterm 5.5 adapter: SerializeAddon preserves cells and public modes,
// but not scrolling margins, saved cursors, tab stops or mouse encoding.
// Keep private API access here and exercise it with real terminal sequences.
function attributes(a) {
  const p = [0];
  for (const [name, code] of [['Bold', 1], ['Dim', 2], ['Italic', 3], ['Underline', 4],
    ['Blink', 5], ['Inverse', 7], ['Invisible', 8], ['Strikethrough', 9], ['Overline', 53]]) {
    if (a['is' + name]()) p.push(code);
  }
  for (const [side, code] of [['Fg', 38], ['Bg', 48]]) {
    const color = a['get' + side + 'Color']();
    if (a['is' + side + 'RGB']()) p.push(code, 2, color >>> 16 & 255, color >>> 8 & 255, color & 255);
    else if (a['is' + side + 'Palette']()) p.push(code, 5, color);
  }
  return '\x1b[' + p.join(';') + 'm';
}
function charset(value) { return value && value.q === '─' ? '0' : 'B'; }
function position(term, publicBuffer, x, y, origin, attr) {
  const buffer = publicBuffer._buffer;
  const row = Math.max(0, y - (origin ? buffer.scrollTop : 0)) + 1;
  let result = '\x1b[' + row + ';' + (Math.min(x, term.cols - 1) + 1) + 'H';
  // CUP cannot represent the pending wrap after the last column. Repaint the
  // final cell to restore that state, then restore the current attributes.
  if (x >= term.cols && term.modes.wraparoundMode) {
    let col = term.cols - 1;
    let cell = publicBuffer.getLine(buffer.ybase + y).getCell(col);
    if (!cell.getWidth()) cell = publicBuffer.getLine(buffer.ybase + y).getCell(--col);
    if (cell.getChars()) {
      result = '\x1b[' + row + ';' + (col + 1) + 'H' + attributes(cell) + cell.getChars() + attributes(attr);
    }
  }
  return result;
}
function bufferState(term, publicBuffer, currentAttr) {
  const b = publicBuffer._buffer;
  let result = '\x1b[?6l\x1b[' + (b.scrollTop + 1) + ';' + (b.scrollBottom + 1) + 'r\x1b[3g';
  for (const key of Object.keys(b.tabs)) {
    const col = Number(key);
    if (b.tabs[key] && col < term.cols) result += '\x1b[1;' + (col + 1) + 'H\x1bH';
  }
  const savedY = Math.max(0, Math.min(term.rows - 1, b.savedY - b.ybase));
  result += attributes(b.savedCurAttrData) + '\x1b(' + charset(b.savedCharset) + '\x0f';
  result += position(term, publicBuffer, b.savedX, savedY, false, b.savedCurAttrData) + '\x1b7';
  result += attributes(currentAttr) + position(term, publicBuffer, b.x, b.y, false, currentAttr);
  return result;
}
function serialize(term, addon, options) {
  const core = term._core, active = term.buffer.active, attr = core._inputHandler._curAttrData;
  let data = addon.serialize({...options, excludeModes: true});
  const split = data.indexOf('\x1b[?1049h\x1b[H');
  if (split >= 0) data = data.slice(0, split)
    + bufferState(term, term.buffer.normal, term.buffer.normal._buffer.savedCurAttrData) + data.slice(split);
  data += bufferState(term, active, attr);
  const m = term.modes;
  for (const [name, code] of [['applicationCursorKeysMode', 1], ['applicationKeypadMode', 66],
    ['bracketedPasteMode', 2004], ['reverseWraparoundMode', 45], ['sendFocusMode', 1004]]) {
    if (m[name]) data += '\x1b[?' + code + 'h';
  }
  if (m.insertMode) data += '\x1b[4h';
  if (!m.wraparoundMode) data += '\x1b[?7l';
  const mouse = {x10: 9, vt200: 1000, drag: 1002, any: 1003}[m.mouseTrackingMode];
  if (mouse) data += '\x1b[?' + mouse + 'h';
  const encoding = {SGR: 1006, SGR_PIXELS: 1016}[core.coreMouseService.activeEncoding];
  if (encoding) data += '\x1b[?' + encoding + 'h';
  const sets = core._charsetService;
  for (let i = 0; i < 4; i++) data += '\x1b' + '()*+'[i] + charset(sets._charsets[i]);
  data += ['\x0f', '\x0e', '\x1bn', '\x1bo'][sets.glevel];
  if (m.originMode) data += '\x1b[?6h' + position(term, active, active.cursorX, active.cursorY, true, attr);
  const style = {block: 1, underline: 3, bar: 5}[term.options.cursorStyle] || 1;
  data += '\x1b[' + (style + (term.options.cursorBlink ? 0 : 1)) + ' q';
  data += '\x1b[?25' + (core.coreService.isCursorHidden ? 'l' : 'h');
  return data;
}
module.exports = {serialize};
