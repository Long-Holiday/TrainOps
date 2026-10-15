// Dependency-free DOM smoke tests for continuous Gantt chart & vertical progress line.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
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
const cli = path.join(root, 'skills/trainops/scripts/trainops.py');
const html = fs.readFileSync(path.join(root, 'skills/trainops/web/index.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
// 演示数据的时间戳是"生成这一刻"推出来的。running 任务一旦超过预计完成时间就变成待估，
// budget() 便按设计整块隐去余额结论（README：任一任务完成时间待估时结论不显示），
// 于是拿仓库里那份快照断言余额块，结果会随提交时间漂移（生成后约 70 分钟内才成立）。
// 这里现生成一份新鲜的演示计划，断言与仓库快照的年龄无关。
const project = fs.mkdtempSync(path.join(os.tmpdir(), 'trainops-web-'));
process.on('exit', () => fs.rmSync(project, {recursive: true, force: true}));
// 与 Python 测试同一约定：测试进程绝不读开发机上的 .env（真 token）。
const env = {...process.env, TRAINOPS_ENV: path.join(project, 'missing.env'), AUTODL_TOKEN: ''};
execFileSync('python3', [cli, '--project', project, 'demo'], {encoding: 'utf8', env});
const data = JSON.parse(execFileSync('python3', [cli, '--project', project, 'show', '--json'],
  {encoding: 'utf8', env}));
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

  // 余额只有一块：金额、代金券标注、按当前开机时价的可跑时长、计划花费与充足结论
  const budgetChips = byClass(nodes.summary, 'budget');
  assert.equal(budgetChips.length, 1, '余额块必须渲染且只有一块');
  assert.match(budgetChips[0].textContent, /余额 ¥128\.50/);
  assert.match(budgetChips[0].textContent, /观测于 (刚刚|\d+ (分钟|小时|天)前)/);
  assert.match(nodes.summary.textContent, /代金券 ¥20\.00（不计入可用时长）/);
  assert.match(nodes.summary.textContent, /现价可跑 37\.1 小时/);
  // 计划剩余时长随真实时间推移而变化，这里只要求它被算出来并带上预估花费
  assert.match(nodes.summary.textContent, /跑完剩余计划 \d+(\.\d+)? GPU 小时≈ ¥\d+\.\d\d/);
  assert.equal(byClass(nodes.summary, 'ok').length, 1, '余额充足时给出明确的结论徽标');
  console.log('PASS: balance and affordable GPU hours are shown in the summary');

  // 自动结算记录：整句文案由服务端写进计划，面板只显示、只按 over 换样式，不在浏览器里重算
  assert.ok(data.settlement && typeof data.settlement.text === 'string',
    '/api/plan 必须带上服务端算好的结算视图');
  assert.equal(byClass(nodes.summary, 'settled').length, 1, '计划里有结算记录就要渲染一块结算徽标');
  assert.match(byClass(nodes.summary, 'settled')[0].textContent, /自动结算/);
  data.settlement = {live:false, over:false, computed_at:new Date().toISOString(),
    text:'自动结算于 3 分钟前 · 跑完剩余计划 4.8 GPU 小时、约 ¥9.62 · 余额够'};
  await nodes.refresh.fire('click');
  assert.equal(byClass(nodes.summary, 'settled-over').length, 0, '结论正常时结算徽标不该报警');
  assert.match(nodes.summary.textContent, /自动结算于 3 分钟前 · 跑完剩余计划 4\.8 GPU 小时、约 ¥9\.62 · 余额够/);
  data.settlement = {live:true, over:true, text:'自动结算已过期（3 小时前，超过 2 个周期）· 当前为实时值 · 上次结算失败'};
  await nodes.refresh.fire('click');
  assert.equal(byClass(nodes.summary, 'settled-over').length, 1, '结算过期/失败时必须用醒目样式');
  console.log('PASS: scheduled settlement is rendered straight from the server-side text');

  // 没有余额观测时整块不渲染：绝不把缺失显示成 0（结算记录同理，一起隐去再看）
  const savedBudget = data.budget;
  const savedSettlement = data.settlement;
  delete data.budget;
  delete data.settlement;
  await nodes.refresh.fire('click');
  assert.equal(byClass(nodes.summary, 'budget').length, 0);
  assert.equal(byClass(nodes.summary, 'settled').length, 0, '没有结算记录时整块不渲染');
  assert.ok(!/¥/.test(nodes.summary.textContent), '缺余额时不该出现任何金额');
  data.budget = savedBudget;
  data.settlement = savedSettlement;
  await nodes.refresh.fire('click');
  assert.equal(byClass(nodes.summary, 'budget').length, 1);
  assert.equal(byClass(nodes.summary, 'settled').length, 1);

  // 面板只做只读展示：没有 TB 链接、没有磁盘条、没有新的外部请求入口
  assert.equal(descendants(nodes.flows, 'a').length + descendants(nodes.summary, 'a').length, 0,
    '面板不提供 TensorBoard 直链');
  assert.ok(!/磁盘|disk_used|tf-logs|tensorboard/i.test(nodes.summary.textContent + nodes.instances.textContent),
    '面板不展示磁盘与 TensorBoard 信息（这些留给 agent 巡检）');

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
