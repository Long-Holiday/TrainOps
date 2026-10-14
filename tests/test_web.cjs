// Dependency-free DOM smoke tests for continuous Gantt chart & vertical progress line.
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
    this.style = {};
    this.classes = new Set();
    this.classList = {add: x => this.classes.add(x), remove: x => this.classes.delete(x), contains: x => this.classes.has(x)};
  }
  set className(value) {
    this._className = String(value);
    this.classes = new Set(this._className.split(/\s+/).filter(Boolean));
  }
  get className() { return this._className || ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ''; this.children = children; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { this[name] = value; }
  fire(name) { return this.listeners[name](); }
}
function descendants(node, tag) {
  return node.children.flatMap(child => [...(child.tagName === tag ? [child] : []), ...descendants(child, tag)]);
}
function byClass(node, cls) {
  return node.children.flatMap(child => [...(child.classList && child.classList.contains(cls) ? [child] : []), ...byClass(child, cls)]);
}
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'skills/trainops/web/index.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const data = JSON.parse(execFileSync('python3', [path.join(root, 'skills/trainops/scripts/trainops.py'),
  '--project', path.join(root, 'examples/demo-project'), 'show', '--json'], {encoding:'utf8'}));
const nodes = Object.fromEntries(['title','updated','instances','summary','flows','error','refresh','theme']
  .map(id => [id, new Element('div')]));
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
  assert.ok(!html.includes('id="search"'), 'Task search input should be removed');
  assert.ok(!html.includes('<details'), 'Task details dropdown should be removed');
  new vm.Script(source, {filename:'index.html'}).runInContext(context);
  await new Promise(resolve => setImmediate(resolve));
  const gantts = byClass(nodes.flows, 'gantt');
  assert.equal(gantts.length, 3, 'Each of the 3 flows must render a Gantt chart');
  assert.equal(byClass(nodes.flows, 'gantt-row').length, 7, 'All 7 tasks must be rendered as Gantt rows');
  assert.equal(descendants(nodes.flows, 'details').length, 0, 'No details element rendered in Gantt rows');
  for (const gantt of gantts) {
    const bars = byClass(gantt, 'gantt-bar').map(bar => {
      const m = bar.style.match(/left:([\d.]+)%;width:([\d.]+)%/);
      return {left: Number(m[1]), width: Number(m[2])};
    });
    assert.equal(bars[0].left, 0, 'First block in flow starts at 0%');
    for (let i = 1; i < bars.length; i++) {
      assert.ok(Math.abs((bars[i - 1].left + bars[i - 1].width) - bars[i].left) < 0.2,
        'Consecutive blocks in a flow must be contiguous without gaps');
    }
  }
  const progLines = byClass(nodes.flows, 'gantt-progress-line');
  assert.equal(progLines.length, 7, 'Each task Gantt bar must have a sub-progress line');
  assert.ok(progLines.some(line => line.style === 'left:18%'), 'F01-T02 sub-progress line should be at 18%');
  const badges = byClass(nodes.flows, 'gantt-progress-badge');
  assert.equal(badges.length, 7, 'Each task Gantt bar must display an unobstructed progress badge');
  assert.ok(badges.some(b => b.textContent === '18%'), 'F01-T02 badge shows 18%');
  const flowTags = byClass(nodes.flows, 'flow-progress-tag');
  assert.equal(flowTags.length, 3, 'Each flow must render a total flow progress red line tag');
  assert.ok(flowTags.every(tag => /^总进度 \d+%$/.test(tag.textContent)), 'Red line tag shows total flow progress');
  assert.equal(nodes.instances.children.length, 2);
  assert.match(nodes.summary.textContent, /3 条流程 \/ 7 个任务/);
  assert.match(nodes.flows.textContent, /F03-T02/);
  assert.match(nodes.flows.textContent, /待估/);
  console.log('PASS: Continuous Gantt charts / no task details / unobstructed badges / total flow progress line');

  nodes.theme.fire('click');
  assert.equal(context.document.documentElement.dataset.theme, 'dark');
  nodes.theme.fire('click');
  assert.equal(context.document.documentElement.dataset.theme, 'light');
  console.log('PASS: theme toggle');

  const barBox = gantt => byClass(gantt, 'gantt-bar').map(bar => {
    const m = bar.style.match(/left:([\d.]+)%;width:([\d.]+)%/);
    return {left: Number(m[1]), width: Number(m[2])};
  });

  // 排程以后端 estimate 为准：后端把节点排到更晚时，条形必须让出空隙，而不是被强行首尾相接
  const later = new Date(Date.parse(data.tasks[2].estimate.start) + 3 * 3600e3).toISOString();
  data.tasks[2].estimate.start = later;
  data.tasks[2].estimate.finish = new Date(Date.parse(later) + 15 * 60000).toISOString();
  await nodes.refresh.fire('click');
  const shifted = barBox(byClass(nodes.flows, 'gantt')[0]);
  // 阈值取 1%：连续条形因 left/width 各自保留一位小数，最多出现约 0.1% 的舍入缝隙，
  // 而本用例制造的空隙占整图一半以上，两者不会混淆。
  const scheduledGap = shifted[2].left - (shifted[1].left + shifted[1].width);
  assert.ok(scheduledGap > 1,
    `Server-scheduled start must win over same-flow contiguity (gap=${scheduledGap.toFixed(1)}%)`);
  console.log('PASS: bar geometry follows the server-side schedule');

  // 每 10 秒重绘一次，但不能把用户正在看的横向滚动位置弹回去
  byClass(nodes.flows, 'gantt-wrap')[0].scrollLeft = 137;
  await nodes.refresh.fire('click');
  assert.equal(byClass(nodes.flows, 'gantt-wrap')[0].scrollLeft, 137,
    'Horizontal scroll must survive the periodic refresh');
  console.log('PASS: gantt horizontal scroll is preserved across refresh');

  // 逾期与巡检超时提示（先清干净，避免演示快照本身随时间变旧影响断言）
  data.tasks.forEach(task => { task.estimate.overdue = false; task.estimate.stale = false; });
  data.tasks[1].estimate.overdue = true;
  data.tasks[1].estimate.stale = true;
  await nodes.refresh.fire('click');
  assert.equal(byClass(nodes.flows, 'eta-overdue').length, 1);
  assert.equal(byClass(nodes.flows, 'eta-stale').length, 1);
  assert.match(nodes.flows.textContent, /已超过预计完成时间，需重新估计/);
  assert.match(nodes.flows.textContent, /超过 1 小时未巡检/);
  console.log('PASS: overdue and never-inspected running tasks are flagged');

  const malicious = '<img src=x onerror="alert(1)">';
  data.name = malicious; data.tasks[0].note = malicious;
  await nodes.refresh.fire('click');
  assert.equal(nodes.title.textContent, malicious);
  assert.equal(descendants(nodes.flows, 'img').length, 0);
  assert.match(nodes.flows.textContent, /<img src=x/);
  console.log('PASS: untrusted plan text is not interpreted as HTML');

  failed = true;
  await nodes.refresh.fire('click');
  assert.match(nodes.error.textContent, /offline/);
  assert.match(nodes.error.textContent, /上次读取的数据/);
  assert.equal(nodes.error.classList.contains('hidden'), false);
  assert.equal(nodes.refresh.disabled, false);
  assert.equal(byClass(nodes.flows, 'gantt-row').length, 7);
  console.log('PASS: offline warning / last snapshot retained / controls recovered');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
