const fs = require('fs');
const s = fs.readFileSync(process.argv[2], 'utf8');
const keywords = process.argv.slice(3);
const out = [];
for (const kw of keywords) {
  let idx = 0;
  let count = 0;
  while (count < 3) {
    const i = s.indexOf(kw, idx);
    if (i === -1) break;
    out.push('\n========== [' + kw + '] @' + i + ' ==========');
    out.push(s.slice(Math.max(0, i - 800), Math.min(s.length, i + 800)));
    idx = i + kw.length;
    count++;
  }
  if (count === 0) out.push('\n========== [' + kw + '] NOT FOUND ==========');
}
fs.writeFileSync(process.argv[2].replace('.js', '') + '-context.txt', out.join('\n'), 'utf8');
console.log('done, sections:', out.length);
