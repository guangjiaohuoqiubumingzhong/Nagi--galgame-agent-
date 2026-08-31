// Unicode font selection for the verified QLIE game copy only.
// Scripts carry real UTF-16 text; no global locale or system fonts are changed.
'use strict';
const gdi = Process.getModuleByName('gdi32.dll');
const fn = (name, ret, args) => new NativeFunction(gdi.getExportByName(name), ret, args, 'stdcall');
const currentObject = fn('GetCurrentObject', 'pointer', ['pointer', 'uint']);
const objectW = fn('GetObjectW', 'int', ['pointer', 'int', 'pointer']);
const fontW = fn('CreateFontIndirectW', 'pointer', ['pointer']);
const select = fn('SelectObject', 'pointer', ['pointer', 'pointer']);
const fonts = new Map();
let mappedCalls = 0;
function font(dc) {
  const lf = Memory.alloc(92);
  if (objectW(currentObject(dc, 6), 92, lf) !== 92) return null;
  const key = Array.from(new Uint8Array(lf.readByteArray(28))).join(',');
  if (!fonts.has(key)) {
    lf.add(23).writeU8(1);
    lf.add(28).writeByteArray(new Uint8Array(64));
    lf.add(28).writeUtf16String('Microsoft YaHei');
    const created = fontW(lf);
    if (created.isNull()) throw new Error('Chinese font creation failed');
    fonts.set(key, created);
  }
  return fonts.get(key);
}
// QLIE rasterizes Unicode glyphs using GetGlyphOutlineW. Cover Unicode drawing
// and measurement too, so text layout and the pixels use the same font.
for (const name of ['GetGlyphOutlineW', 'TextOutW', 'ExtTextOutW', 'GetTextExtentPoint32W']) {
  Interceptor.attach(gdi.getExportByName(name), {
    onEnter(args) {
      // GGO_GLYPH_INDEX / ETO_GLYPH_INDEX refer to the original font's glyphs.
      if ((name === 'GetGlyphOutlineW' && (args[2].toUInt32() & 0x80)) ||
          (name === 'ExtTextOutW' && (args[3].toUInt32() & 0x10))) return;
      this.dc = args[0];
      const chosen = font(this.dc);
      if (chosen) {
        this.previous = select(this.dc, chosen);
        mappedCalls++;
      }
    },
    onLeave() { if (this.previous && !this.previous.isNull()) select(this.dc, this.previous); }
  });
}
rpc.exports = {status() { return {mappedCalls, fonts:fonts.size, engine:'qlie-unicode'}; }};
send({type:'ready', pid:Process.id, architecture:Process.arch, engine:'qlie-unicode'});
