const fs = require('fs');
const file = process.argv[2];
const s = fs.readFileSync(file, 'utf8');

// Extract all Chinese-containing string literals (quoted)
const re = /["'`]([^"'`\\]*[一-鿿][^"'`\\]*)["'`]/g;
let m;
const seen = new Map();
while ((m = re.exec(s)) !== null) {
  let t = m[1].trim();
  if (t.length < 2 || t.length > 300) continue;
  // skip pure css/class junk
  if (/^[a-z0-9\-_ :;.%,#()]+$/i.test(t)) continue;
  if (!seen.has(t)) seen.set(t, 0);
  seen.set(t, seen.get(t) + 1);
}
const out = [...seen.entries()].sort((a, b) => b[1] - a[1]);
console.log(`=== ${file} : ${out.length} unique strings ===`);
out.forEach(([t, c]) => console.log(`${c > 1 ? c + 'x ' : ''}${t}`));
