const fs = require('fs');
const s = fs.readFileSync('e:/OmniSpace/docs/competitor/index.js', 'utf8');
// find all /sapi paths
const re = /["'`](\/sapi\/[^"'`]+)["'`]/g;
let m; const set = new Set();
while ((m = re.exec(s)) !== null) set.add(m[1]);
// find method definitions near url: patterns
const re2 = /(\w+)\(([^)]{0,80})\)\{return \w+\(\{url:`([^`]+)`[^}]*method:"(\w+)"/g;
const defs = [];
while ((m = re2.exec(s)) !== null) defs.push(m[4].toUpperCase() + ' ' + m[3] + '  <- ' + m[1] + '(' + m[2] + ')');
const re3 = /async (\w+)\(([^)]{0,80})\)\{return \w+\(\{url:`([^`]+)`[^}]*method:"(\w+)"/g;
while ((m = re3.exec(s)) !== null) defs.push(m[4].toUpperCase() + ' ' + m[3] + '  <- async ' + m[1] + '(' + m[2] + ')');
const out = ['=== /sapi paths ===', ...[...set].sort(), '', '=== method defs (' + defs.length + ') ===', ...defs];
fs.writeFileSync('e:/OmniSpace/docs/competitor/api-full.txt', out.join('\n'), 'utf8');
console.log('paths:', set.size, 'defs:', defs.length);
