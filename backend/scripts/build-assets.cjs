/** Copy locked npm distributions into Django's static discovery directory. */
const { copyFileSync, mkdirSync, readFileSync, writeFileSync } = require('node:fs');
const { resolve, dirname } = require('node:path');
const root = resolve(__dirname, '..');
const assets = {
  '@tabler/core/dist/css/tabler.min.css': 'tabler/tabler.min.css',
  '@tabler/core/dist/js/tabler.min.js': 'tabler/tabler.min.js',
  'htmx.org/dist/htmx.min.js': 'htmx/htmx.min.js',
  'alpinejs/dist/cdn.min.js': 'alpine/alpine.min.js',
  'apexcharts/dist/apexcharts.min.js': 'apexcharts/apexcharts.min.js',
};
for (const [source, target] of Object.entries(assets)) {
  const destination = resolve(root, 'static/vendor', target);
  mkdirSync(dirname(destination), { recursive: true });
  copyFileSync(resolve(root, 'node_modules', source), destination);
}
console.log('Frontend assets prepared for Django collectstatic.');

// Ship only the official outline icons used by the public components.
const iconNames = [
  'users', 'file-certificate', 'clipboard-check', 'shield-check',
  'file-description', 'receipt', 'chart-bar', 'layout-dashboard',
  'arrows-exchange', 'building', 'download', 'upload', 'building-bank', 'briefcase',
];
const iconDirectory = resolve(root, 'static/vendor/tabler-icons');
mkdirSync(iconDirectory, { recursive: true });
const symbols = iconNames.map((name) => {
  const source = readFileSync(resolve(root, `node_modules/@tabler/icons/icons/outline/${name}.svg`), 'utf8');
  const body = source.slice(source.indexOf('>', source.indexOf('<svg')) + 1, source.lastIndexOf('</svg>'));
  return `<symbol id="${name}" viewBox="0 0 24 24">${body}</symbol>`;
});
writeFileSync(resolve(iconDirectory, 'sprite.svg'), `<svg xmlns="http://www.w3.org/2000/svg">${symbols.join('')}</svg>`);
copyFileSync(resolve(root, 'node_modules/@tabler/icons/LICENSE'), resolve(iconDirectory, 'LICENSE'));
