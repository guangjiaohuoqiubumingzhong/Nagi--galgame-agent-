// Experimental Unicode drawing bridge, restricted by the host to the game copy.
// No text extraction: restore reserved CP932 glyphs from this deployment's translations.
'use strict';
const mapping = PILOT_GLYPH_MAP;
const gdi = Process.getModuleByName('gdi32.dll');
const kernel = Process.getModuleByName('kernel32.dll');
const api = (module, name, result, args) => new NativeFunction(module.getExportByName(name), result, args, 'stdcall');
const toWide = api(kernel, 'MultiByteToWideChar', 'int', ['uint','uint','pointer','int','pointer','int']);
const textOutW = api(gdi, 'TextOutW', 'int', ['pointer','int','int','pointer','int']);
const getCurrentObject = api(gdi, 'GetCurrentObject', 'pointer', ['pointer','uint']);
const getObjectW = api(gdi, 'GetObjectW', 'int', ['pointer','int','pointer']);
const createFontW = api(gdi, 'CreateFontIndirectW', 'pointer', ['pointer']);
const selectObject = api(gdi, 'SelectObject', 'pointer', ['pointer','pointer']);
const textOutPtr = gdi.getExportByName('TextOutA');
let originalTextOut;
const fonts = new Map();
let mappedCalls = 0;

function chineseFont(dc) {
  const current = getCurrentObject(dc, 6);
  const lf = Memory.alloc(92);
  if (getObjectW(current, 92, lf) !== 92) return null;
  const key = Array.from(new Uint8Array(lf.readByteArray(28))).join(',');
  let font = fonts.get(key);
  if (!font) {
    lf.add(23).writeU8(1); // DEFAULT_CHARSET; Unicode code points select glyphs.
    lf.add(28).writeByteArray(new Uint8Array(64));
    lf.add(28).writeUtf16String('Microsoft YaHei');
    font = createFontW(lf);
    if (font.isNull()) return null;
    fonts.set(key, font);
  }
  return font;
}

const replacement = new NativeCallback(function(dc, x, y, input, length) {
  if (length > 0 && length <= 8192 && !input.isNull()) {
    const wide = Memory.alloc((length + 1) * 2);
    const count = toWide(932, 0, input, length, wide, length + 1);
    if (count > 0) {
      const text = wide.readUtf16String(count);
      let changed = false;
      const converted = Array.from(text, c => {
        if (Object.prototype.hasOwnProperty.call(mapping, c)) {
          changed = true;
          return mapping[c];
        }
        return c;
      }).join('');
      if (changed) {
        const font = chineseFont(dc);
        const previous = font ? selectObject(dc, font) : null;
        let result;
        try {
          const output = Memory.allocUtf16String(converted);
          result = textOutW(dc, x, y, output, converted.length);
        } finally {
          if (previous) selectObject(dc, previous);
        }
        mappedCalls++;
        if (mappedCalls <= 100) send({type:'mapped_glyph',text:converted,ok:result !== 0});
        return result;
      }
    }
  }
  return originalTextOut(dc, x, y, input, length);
}, 'int', ['pointer','int','int','pointer','int'], 'stdcall');
originalTextOut = new NativeFunction(Interceptor.replaceFast(textOutPtr, replacement),
  'int', ['pointer','int','int','pointer','int'], 'stdcall');
rpc.exports = {status() { return {mappedCalls,fonts:fonts.size}; }};
send({type:'ready',pid:Process.id,architecture:Process.arch,glyphs:Object.keys(mapping).length});
