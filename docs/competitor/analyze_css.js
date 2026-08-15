const fs = require('fs');
const s = fs.readFileSync(process.argv[2], 'utf8');

// 1. CSS custom properties
const vars = {};
const varRe = /--([a-zA-Z0-9-]+)\s*:\s*([^;}]+)/g;
let m;
while ((m = varRe.exec(s)) !== null) vars['--' + m[1]] = m[2].trim();

// 2. All colors frequency
const colors = {};
const colorRe = /#[0-9a-fA-F]{3,8}\b|rgba?\([^)]+\)/g;
while ((m = colorRe.exec(s)) !== null) {
  const c = m[0].toLowerCase();
  colors[c] = (colors[c] || 0) + 1;
}
const topColors = Object.entries(colors).sort((a, b) => b[1] - a[1]).slice(0, 40);

// 3. Class names frequency (layout hints)
const classes = {};
const classRe = /\.([a-zA-Z][a-zA-Z0-9_-]{2,40})/g;
while ((m = classRe.exec(s)) !== null) {
  classes[m[1]] = (classes[m[1]] || 0) + 1;
}
const topClasses = Object.entries(classes).sort((a, b) => b[1] - a[1]).slice(0, 120);

const lines = [];
lines.push('=== CSS VARIABLES ===');
Object.entries(vars).forEach(([k, v]) => lines.push(k + ': ' + v));
lines.push('\n=== TOP COLORS ===');
topColors.forEach(([c, n]) => lines.push(n + 'x ' + c));
lines.push('\n=== TOP CLASSES ===');
topClasses.forEach(([c, n]) => lines.push(n + 'x .' + c));
fs.writeFileSync(process.argv[3], lines.join('\n'), 'utf8');
console.log('written', process.argv[3], lines.length, 'lines');
