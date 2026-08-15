const fs = require('fs');
const files = ['e:/OmniSpace/docs/competitor/index.js', 'e:/OmniSpace/docs/competitor/cartoonDetail-iIMagjQK.js', 'e:/OmniSpace/docs/competitor/cartoon-Dfkk3An0.js'];
const names = ['scriptReasoning', 'roleGenerate', 'aiGenerate', 'aiGenerateResources', 'operationResources', 'resourcesHistoryList', 'retryGenerateVideo', 'delResourcesHistory', 'editBackupResources', 'addBackupResources', 'useBackupResources', 'operateModel', 'fetchCartoonModels', 'calcPointDesc', 'worksInfo', 'listByWorksId'];
const out = [];
for (const f of files) {
  const s = fs.readFileSync(f, 'utf8');
  for (const n of names) {
    const i = s.indexOf(n + ':');
    const i2 = s.indexOf(n + '=');
    const i3 = s.indexOf(n + '(');
    for (const idx of [i, i2]) {
      if (idx > -1) {
        out.push('--- [' + n + '] in ' + f.split(/[\\/]/).pop() + ' @' + idx);
        out.push(s.slice(Math.max(0, idx - 60), Math.min(s.length, idx + 320)).replace(/\n/g, ' '));
        break;
      }
    }
  }
  // also grab any http-ish or api-ish strings
  const re = /["'`]([a-zA-Z0-9_/.-]*(?:works|cartoon|resources|storyboard|subject|task|model)[a-zA-Z0-9_/.-]*)["'`]/g;
  let m; const set = new Set();
  while ((m = re.exec(s)) !== null) { const p = m[1]; if (p.length > 6 && (p.includes('/') || p.includes('api'))) set.add(p); }
  out.push('=== api-ish strings in ' + f.split(/[\\/]/).pop() + ' ===');
  out.push([...set].slice(0, 120).join('\n'));
}
fs.writeFileSync('e:/OmniSpace/docs/competitor/api-detail.txt', out.join('\n'), 'utf8');
console.log('done');
