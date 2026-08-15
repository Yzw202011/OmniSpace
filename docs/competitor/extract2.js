const fs = require('fs');
const file = process.argv[2];
const outFile = process.argv[3];
const s = fs.readFileSync(file, 'utf8');
const CJK = '[\\u4e00-\\u9fff\\u3000-\\u303f\\uff00-\\uffef]';
const re = new RegExp('["\'`]([^"\'`\\\\]{0,300}' + CJK + '[^"\'`\\\\]{0,300})["\'`]', 'g');
let m;
const seen = new Map();
while ((m = re.exec(s)) !== null) {
  let t = m[1].trim();
  if (t.length < 2) continue;
  if (!seen.has(t)) seen.set(t, 0);
  seen.set(t, seen.get(t) + 1);
}
const arr = [...seen.keys()].sort((a, b) => a.length - b.length);
fs.writeFileSync(outFile, arr.join('\n'), 'utf8');
console.log(file.split(/[\\/]/).pop(), '-> extracted:', arr.length);
