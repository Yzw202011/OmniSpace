const fs = require('fs');
const out = [];
for (const f of process.argv.slice(2)) {
  const s = fs.readFileSync(f, 'utf8');
  const re = /["'`]((?:\/[a-zA-Z0-9_-]+){2,}\/?)["'`]/g;
  let m;
  const set = new Set();
  while ((m = re.exec(s)) !== null) {
    const p = m[1];
    if (/\.(js|css|png|svg|jpg|woff)/.test(p)) continue;
    if (p.includes('static')) continue;
    set.add(p);
  }
  out.push('=== ' + f.split(/[\\/]/).pop() + ' ===');
  out.push([...set].sort().join('\n'));
}
fs.writeFileSync('e:/OmniSpace/docs/competitor/api-endpoints.txt', out.join('\n'), 'utf8');
console.log('done');
