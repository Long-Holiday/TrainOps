// Dependency-free DOM smoke tests, not a substitute for browser layout checks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.classes = new Set();
    this.value = '';
    this.open = false;
    this.classList = {add: x => this.classes.add(x), remove: x => this.classes.delete(x), contains: x => this.classes.has(x)};
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ''; this.children = children; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { this[name] = value; }
  fire(name) { return this.listeners[name](); }
}
function descendants(node, tag) {
  return node.children.flatMap(child => [ ...(child.tagName === tag ? [child] : []), ...descendants(child, tag)]);
}
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'skills/trainops/web/index.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const data = JSON.parse(execFileSync('python3', [path.join(root, 'skills/trainops/scripts/trainops.py'),
  '--project', path.join(root, 'examples/demo-project'), 'show', '--json'], {encoding:'utf8'}));
const nodes = Object.fromEntries(['title','updated','instances','monitor','summary','search','status-filter','flows','error','refresh','theme']
  .map(id => [id, new Element('div')]));
nodes['status-filter'].value = 'all';
let failed = false;
const storage = {};
const context = vm.createContext({
  document: {getElementById: id => nodes[id], createElement: tag => new Element(tag),
    createTextNode: text => { const node = new Element('#text'); node.textContent = text; return node; },
    documentElement: new Element('html')},
  navigator: {clipboard: {writeText: async () => {}}},
  localStorage: {getItem: key => storage[key], setItem: (key,value) => {storage[key] = value;}},
  matchMedia: () => ({matches:false}),
  fetch: async () => { if (failed) throw new Error('offline'); return {ok:true,json:async () => data}; },
  AbortController, Date, console,
  setTimeout: () => 1, clearTimeout: () => {}, setInterval: () => 1,
});

async function main() {
  new vm.Script(source, {filename:'index.html'}).runInContext(context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(descendants(nodes.flows,'tbody').reduce((count,body) => count + body.children.length,0),7);
  assert.equal(nodes.instances.children.length,2);
  assert.match(nodes.summary.textContent,/3 条流程 \/ 7 个任务/);
  assert.match(nodes.flows.textContent,/F03-T02/);
  assert.match(nodes.flows.textContent,/待估/);
  console.log('PASS: initial render / instances / dependencies / unknown ETA');

  nodes.search.value = 'F01-T02'; nodes.search.fire('input');
  assert.equal(descendants(nodes.flows,'tbody')[0].children.length,1);
  assert.match(nodes.flows.textContent,/基线训练/);
  nodes.search.value = ''; nodes.search.fire('input');
  nodes['status-filter'].value = 'running'; nodes['status-filter'].fire('change');
  assert.equal(descendants(nodes.flows,'tbody').reduce((count,body) => count + body.children.length,0),2);
  nodes['status-filter'].value = 'all'; nodes['status-filter'].fire('change');
  console.log('PASS: task ID search / state filter');

  const details = descendants(nodes.flows,'details')[1];
  details.open = true; details.fire('toggle');
  await nodes.refresh.fire('click');
  assert.equal(descendants(nodes.flows,'details')[1].open,true);
  nodes.theme.fire('click');
  assert.equal(context.document.documentElement.dataset.theme,'dark');
  nodes.theme.fire('click');
  assert.equal(context.document.documentElement.dataset.theme,'light');
  console.log('PASS: refresh keeps expanded record / theme toggle');

  const malicious = '<img src=x onerror="alert(1)">';
  data.name = malicious; data.tasks[0].note = malicious;
  await nodes.refresh.fire('click');
  assert.equal(nodes.title.textContent,malicious);
  assert.equal(descendants(nodes.flows,'img').length,0);
  assert.match(nodes.flows.textContent,/<img src=x/);
  console.log('PASS: untrusted plan text is not interpreted as HTML');

  failed = true;
  await nodes.refresh.fire('click');
  assert.match(nodes.error.textContent,/offline/);
  assert.match(nodes.error.textContent,/上次读取的数据/);
  assert.equal(nodes.error.classList.contains('hidden'),false);
  assert.equal(nodes.refresh.disabled,false);
  assert.equal(descendants(nodes.flows,'tbody').reduce((count,body) => count + body.children.length,0),7);
  console.log('PASS: offline warning / last snapshot retained / controls recovered');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
