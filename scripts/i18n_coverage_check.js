#!/usr/bin/env node
// EN_MAP coverage check for webui/static/index.html
// 1) every static t('...') key and data-i18n* attr must exist in EN_MAP (else: untranslated in EN mode)
// 2) dynamic key sources (R2V_EXAMPLES[].name, R2V_TOKENS[].tip/name) must exist in EN_MAP
// 3) EN_MAP keys not referenced anywhere (dead entries, informational)
'use strict';
const fs = require('fs');
const path = process.argv[2] || 'webui/static/index.html';
const html = fs.readFileSync(path, 'utf8');

// extract main script block
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.error('no script block'); process.exit(2); }
const js = m[1];

// extract EN_MAP literal (from 'const EN_MAP={' to the ';' after the closing '}' before 'let LANG')
const start = js.indexOf('const EN_MAP={');
const langIdx = js.indexOf('let LANG');
if (start < 0 || langIdx < 0) { console.error('EN_MAP not found'); process.exit(2); }
const mapSrc = js.slice(start + 'const EN_MAP='.length, js.lastIndexOf('}', langIdx) + 1);
let EN_MAP;
try { EN_MAP = eval('(' + mapSrc + ')'); } catch (e) { console.error('EN_MAP parse fail:', e.message); process.exit(2); }
const mapKeys = new Set(Object.keys(EN_MAP));

// static t('...') keys
const used = new Set();
const re = /(^|[^\w$])t\('((?:[^'\\]|\\.)*)'\s*[,)]/g;
let mm, dynCalls = 0;
while ((mm = re.exec(js))) { try { used.add(eval("('" + mm[2] + "')")); } catch (_) { used.add(mm[2]); } }
// count dynamic t( calls (non-literal arg) for info
const reDyn = /(^|[^\w$])t\((?!')/g;
while ((mm = reDyn.exec(js))) dynCalls++;

// data-i18n* attributes
const reAttr = /data-i18n(?:-ph|-title)?="([^"]*)"/g;
const attrs = new Set();
while ((mm = reAttr.exec(html))) attrs.add(mm[1]);

// dynamic key sources
function extractArr(name) {
  const s = js.indexOf('const ' + name + '=[');
  if (s < 0) return [];
  // naive bracket matching to end of array
  let depth = 0, i = s + ('const ' + name + '=').length;
  for (; i < js.length; i++) {
    if (js[i] === '[') depth++;
    else if (js[i] === ']') { depth--; if (!depth) break; }
  }
  try { return eval('(' + js.slice(s + ('const ' + name + '=').length, i + 1) + ')'); }
  catch (e) { console.error(name, 'parse fail', e.message); return []; }
}
const examples = extractArr('R2V_EXAMPLES');
const tokens = extractArr('R2V_TOKENS');

let missing = 0;
console.log('== EN_MAP size:', mapKeys.size, '| static t() keys:', used.size, '| data-i18n* attrs:', attrs.size, '| dynamic t() calls:', dynCalls);

for (const k of used) if (!mapKeys.has(k)) { console.log('MISSING t() key:', JSON.stringify(k)); missing++; }
for (const k of attrs) if (!mapKeys.has(k)) { console.log('MISSING data-i18n attr key:', JSON.stringify(k)); missing++; }
for (const ex of examples) if (ex.name && !mapKeys.has(ex.name)) { console.log('MISSING example name key:', JSON.stringify(ex.name)); missing++; }
for (const tk of tokens) { if (tk.tip && !mapKeys.has(tk.tip)) { console.log('MISSING token tip key:', JSON.stringify(tk.tip)); missing++; } }

// dead keys (in map, referenced by nothing static; note dynamic lookups use example names / token tips)
const dynKeySet = new Set();
for (const ex of examples) if (ex.name) dynKeySet.add(ex.name);
for (const tk of tokens) { if (tk.tip) dynKeySet.add(tk.tip); }
const dead = [...mapKeys].filter(k => !used.has(k) && !attrs.has(k) && !dynKeySet.has(k));

console.log('missing keys:', missing);
console.log('dead EN_MAP entries (unused, may be intentional):', dead.length);
if (dead.length) console.log('  ' + dead.slice(0, 20).map(k => JSON.stringify(k)).join(', ') + (dead.length > 20 ? ' …' : ''));
// duplicate literal keys in source would silently override — check
const keyRe = /'(?:[^'\\]|\\.)*'\s*:/g;
const seen = new Map(); const dups = [];
for (const k of mapKeys) seen.set(k, (seen.get(k) || 0));
const srcKeys = mapSrc.match(keyRe) || [];
const counts = new Map();
for (const raw of srcKeys) { const key = eval('(' + raw.slice(0, raw.lastIndexOf(':')) + ')'); counts.set(key, (counts.get(key) || 0) + 1); }
for (const [k, c] of counts) if (c > 1) dups.push(k + ' x' + c);
console.log('duplicate EN_MAP literal keys (later wins):', dups.length ? dups.join(', ') : 'none');
process.exit(missing ? 1 : 0);
