// Dependency-free DOM smoke tests for the execution queue: the server computes
// lane order and task.view.progress; the panel renders one scrollable queue.
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
  focus() {}
  fire(name, event) { return this.listeners[name](event); }
}
function descendants(node, tag) {
  return node.children.flatMap(child => [...(child.tagName === tag ? [child] : []), ...descendants(child, tag)]);
}
function byClass(node, cls) {
  return node.children.flatMap(child => [...(child.classList && child.classList.contains(cls) ? [child] : []), ...byClass(child, cls)]);
}
function taskById(node, id) {
  return byClass(node, 'task-node').find(card => card.id === 'task-' + id);
}
const root = path.resolve(__dirname, '..');
const cli = path.join(root, 'skills/trainops/scripts/trainops.py');
const html = fs.readFileSync(path.join(root, 'skills/trainops/web/index.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const webSource = fs.readFileSync(path.join(root, 'skills/trainops/scripts/trainops_web.py'), 'utf8');
// 现生成一份新鲜的演示计划，断言与仓库快照的生成时间无关。
const project = fs.mkdtempSync(path.join(os.tmpdir(), 'trainops-web-'));
process.on('exit', () => fs.rmSync(project, {recursive: true, force: true}));
execFileSync('python3', [cli, '--project', project, 'demo'], {encoding: 'utf8'});
const data = JSON.parse(execFileSync('python3', [cli, '--project', project, 'show', '--json'],
  {encoding: 'utf8'}));
// 面板只消费服务端下发的 task.view.progress。这里显式装一份演示进度，
// 断言与旧快照是否还带 graph 无关，也便于在后端切换快照结构前先跑通前端。
const demoProgress = {'F01-T01':100,'F01-T02':18,'F02-T01':15};
for (const task of data.tasks) {
  task.view = {...(task.view || {}), progress:demoProgress[task.id] ?? 0};
}
const nodes = Object.fromEntries(['title','updated','instances','summary','diagram','error','refresh','theme']
  .map(id => [id, new Element('div')]));
let failed = false;
const calls = [];              // 面板发出的每个请求，用来断言它只发只读请求
const storage = {};
const context = vm.createContext({
  document: {getElementById: id => nodes[id], createElement: tag => new Element(tag),
    createTextNode: text => { const node = new Element('#text'); node.textContent = text; return node; },
    documentElement: new Element('html')},
  navigator: {clipboard: {writeText: async () => {}}},
  localStorage: {getItem: key => storage[key], setItem: (key,value) => {storage[key] = value;}},
  matchMedia: () => ({matches:false}),
  fetch: async (url, options = {}) => {
    calls.push({url, options});
    if (failed) throw new Error('offline');
    return {ok:true,json:async () => data};
  },
  AbortController, Date, console,
  setTimeout: () => 1, clearTimeout: () => {}, setInterval: () => 1,
});

async function main() {
  assert.ok(!html.includes('id="search"'), 'Task search input should be removed');
  assert.ok(!html.includes('<details'), 'Task details dropdown should be removed');
  // 面板只读：Token/时价登记、余额、结算及其请求入口都已删除。
  for (const gone of ['id="token"','id="modal"','/api/token','/api/instance-price','Token','AutoDL',
    '余额','代金券','时价','结算','budget','settlement'])
    assert.ok(!html.includes(gone), `${gone} 已随计费功能删除`);
  for (const gone of ['openDialog','closeDialog','priceButton','openPrice','budgetNodes',
    'settlementNode','renderTokenButton','credentials','money('])
    assert.ok(!source.includes(gone), `前端不再包含 ${gone}`);
  assert.ok(!source.includes("method:'POST'"), '面板不再发写请求');
  // 服务端只剩本机只读面板：没有 AutoDL 调用、凭据、余额/结算线程或写端点。
  for (const gone of ['AutoDL','autodl','urllib.request','urllib.error','Credential','api_post','balance_amounts','wallet',
    'price','Token','token','settlement','budget','background_monitor','do_POST','do_PUT','do_PATCH',
    'do_DELETE','PRICE_PATH','TOKEN_PATH'])
    assert.ok(!webSource.includes(gone), `trainops_web.py 不应再包含 ${gone}`);
  assert.match(webSource, /def server\(store, port=8765\)/, 'server 只接收本地 Store');
  assert.match(webSource, /def local\(self\)/, '保留本机 Host 校验');
  assert.match(webSource, /self\.headers\.get\("Host"/, 'Host 白名单不能删');
  assert.equal((webSource.match(/def do_/g) || []).length, 1, '只保留 GET 读路径');
  // DOM 桩不做排版：同时锁定完整名称所需的 CSS，避免文本在 DOM 中完整但仍被省略。
  const taskNameCSS = html.match(/\.task-name\s*\{([^}]+)\}/)[1];
  assert.match(taskNameCSS, /white-space:\s*normal/, '任务名称必须允许换行');
  assert.match(taskNameCSS, /overflow-wrap:\s*anywhere/, '连续英文/下划线名称也必须能换行');
  assert.ok(!/text-overflow:\s*ellipsis|overflow:\s*hidden/.test(taskNameCSS), '任务名称不能裁剪或省略');
  const taskTopCSS = html.match(/\.task-top\s*\{([^}]+)\}/)[1];
  assert.match(taskTopCSS, /flex-wrap:\s*wrap/, '窄屏状态必须能换行，不能挤掉名称');
  // .notice 与 .hidden 同为一条类选择器，靠后写的赢，空错误条会整条盖在读数条上方
  assert.ok(html.includes('.notice.hidden'), '关闭态必须有 .notice.hidden 压过 .notice 的 display:flex');
  new vm.Script(source, {filename:'index.html'}).runInContext(context);
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(nodes.diagram.textContent.includes('任务执行队列'), '默认显示执行队列');
  assert.ok(nodes.diagram.textContent.includes('不是前置依赖'), '排队连线要说明不是依赖');
  // 单视图：不再有视图切换、依赖图画布与几何走线
  for (const gone of ['diagramMode','diagramSwitch','renderDiagram','plan.graph',
    'CANVAS_PAD','NODE_W','COL_PITCH','ROW_PITCH','PORT_Y'])
    assert.ok(!source.includes(gone), `${gone} 已随依赖图视图删除`);
  for (const gone of ['canvas','diagram-switch','diagram-view'])
    assert.ok(!html.includes(gone), `${gone} 的样式与节点已删除`);
  const queueHeading = byClass(nodes.diagram, 'diagram-header')[0];
  assert.deepEqual(queueHeading.children.map(node => node.className), ['diagram-title'],
    '队列标题栏只有标题，不再提供视图切换');
  assert.equal(byClass(nodes.diagram, 'diagram-switch').length, 0, '不再有视图切换按钮');
  assert.equal(byClass(nodes.diagram, 'diagram-view').length, 0, '不再有依赖图视图按钮');
  assert.equal(byClass(nodes.diagram, 'canvas-edge').length, 0, '不再画依赖连线');
  assert.equal(byClass(nodes.diagram, 'canvas-node').length, 0, '不再绝对定位任务节点');
  assert.equal(byClass(nodes.diagram, 'queue-viewport').length, 1, '执行队列要有可滚动视口');
  assert.equal(byClass(nodes.diagram, 'start-node').length, 0, '不显示开始节点');
  const taskNodes = byClass(nodes.diagram, 'task-node');
  assert.equal(taskNodes.length, 7, 'All tasks appear exactly once');
  assert.equal(new Set(taskNodes.map(node => node.id)).size, 7, '任务节点按 ID 唯一');
  assert.ok(taskNodes.every(node => node.classList.contains('queue-node')), '任务节点使用队列布局');
  assert.equal(descendants(nodes.diagram, 'details').length, 0);
  assert.ok(taskNodes.every(node => byClass(node, 'task-node').length === 0),
    '每个任务节点都是叶子，不包含子任务');
  assert.ok(taskNodes.every(node => descendants(node, 'a').length === 0),
    '队列不再在任务节点里塞外部引用列表');
  assert.ok(!nodes.diagram.textContent.includes('前置（全部完成）'), '任务节点不再列出子任务/前置清单');
  const lanes = byClass(nodes.diagram, 'queue-lane');
  assert.equal(lanes.length, 4, '两台实例、本地任务与已结束任务各成一条车道');
  const queueEdges = byClass(nodes.diagram, 'queue-edge');
  assert.equal(queueEdges.length, 2, '同一实例内的候选顺序用虚线连接');
  assert.deepEqual(queueEdges.map(edge => [edge.dataset.source,edge.dataset.target]),
    [['F01-T02','F01-T03'],['F02-T01','F02-T02']], '排队箭头按服务端 lane 顺序连接相邻任务');
  assert.ok(queueEdges.every(edge => edge['aria-label'].includes('非硬依赖')),
    '排队箭头明确标注不是硬依赖');
  assert.deepEqual(byClass(nodes.diagram, 'queue-label').map(node => node.textContent),
    ['I01 基线 / GPU A','I02 消融 / GPU B','本地任务（不独占 GPU，不表示串行）','已结束 / 需处理（不在执行队列）'],
    '车道顺序与标签直接取服务端视图');
  const nodeCSS = html.match(/\.task-node\s*\{([^}]+)\}/)[1];
  assert.ok(!/(?:^|;)\s*(?:max-)?height\s*:/.test(nodeCSS), '节点随正文变高，不能固定或限制高度');
  assert.match(nodeCSS, /border-left-width:\s*6px/, '任务节点色带要足够醒目');
  assert.match(html.match(/\.instance\s*\{([^}]+)\}/)[1], /border-left:6px solid var\(--inst-color/,
    '实例行色带与任务节点同宽');
  assert.match(html, /\.queue-viewport\s*\{[^}]*overflow:auto/, '队列视口用原生滚动查看长队列');
  assert.ok(!/\.queue-viewport\s*\{[^}]*cursor:grab/.test(html), '不用自定义拖拽平移');
  assert.match(html, /\.queue-edge\s*\{[^}]*dashed/, '排队连线是虚线');
  assert.equal((html.match(/--inst-0:/g) || []).length, 2, '实例身份色在深浅主题各有一套');
  const instanceColors = [...html.matchAll(/--inst-\d+:(#[0-9a-f]{6})/g)].map(match => match[1]);
  assert.equal(instanceColors.length, 10, '五个实例色在深浅主题各有一套');
  for (const color of instanceColors) {
    const [, red, green, blue] = color.match(/^#(..)(..)(..)$/).map(value => parseInt(value, 16));
    assert.ok(!(green > red && green > blue), `实例身份色 ${color} 不能偏绿，避免和已完成节点撞色`);
  }
  assert.match(html, /\.task-node\.running\s*\{[^}]*var\(--inst-color/,
    '执行中节点使用所属实例的身份色');
  assert.ok(!/linear-gradient|\.legend-item|\.flow-meter/.test(html),
    '不保留渐变、图例与流程进度读数');
  assert.ok(queueEdges.every(edge => !edge.className.includes('inst-')),
    '排队连线保持中性色，不按实例着色');
  assert.equal(byClass(nodes.diagram, 'instance-tag').length, 7, '每个任务节点都标出所在实例');
  assert.equal(byClass(nodes.diagram, 'legend-item').length, 0, '不画实例颜色图例');
  for (const forbidden of ['getBoundingClientRect', 'ResizeObserver', 'requestAnimationFrame',
    'infinite', '<canvas', 'gantt-', 'laneLeft', 'innerHTML'])
    assert.ok(!html.includes(forbidden), `执行队列不能包含 ${forbidden}`);
  assert.ok(!/<(?:script|link)[^>]+(?:src|href)=["']https?:/.test(html), 'No external assets');
  const values = byClass(nodes.diagram, 'task-progress-value');
  assert.equal(values.length, 7);
  const progressTask = taskById(nodes.diagram, 'F01-T02');
  assert.equal(byClass(progressTask, 'task-progress-value')[0].textContent, '18%',
    '进度直接取 task.view.progress');
  assert.equal(byClass(progressTask, 'task-meter-fill')[0].style, 'width:18%',
    '进度条宽度不做前端换算');
  assert.equal(byClass(nodes.diagram, 'task-meter-fill').length, 7);
  assert.equal(byClass(nodes.diagram, 'flow-item').length, 3, '每条流程一条纯文本读数');
  assert.equal(descendants(nodes.diagram, 'a').length, 0, '执行队列不需要跨流程跳转链接');
  assert.equal(nodes.instances.children.length, 2);
  const instanceRows = byClass(nodes.instances, 'instance');
  assert.deepEqual(instanceRows.map(row => [...row.classes].find(name => name.startsWith('inst-color-'))),
    ['inst-color-0','inst-color-1'], '实例行标出身份色');
  assert.ok(instanceRows.every(row => byClass(row, 'status').length === 1), '实例行只保留状态');
  assert.equal(descendants(nodes.instances, 'button').length, 0, '实例行没有时价等写入口');
  assert.ok(!/¥|余额|时价/.test(nodes.instances.textContent), '实例行不展示任何金额');
  const runningNodes = taskNodes.filter(node => node.classList.contains('running'));
  assert.equal(runningNodes.length, 2, '演示计划有两个执行中任务');
  assert.deepEqual(runningNodes.map(node => [...node.classes].find(name => name.startsWith('inst-color-'))),
    ['inst-color-0','inst-color-1'], '执行中节点用所属实例的同一身份色');
  assert.ok(taskNodes.filter(node => !node.classList.contains('running'))
    .every(node => ![...node.classes].some(name => name.startsWith('inst-color-'))),
    '非执行中节点不按实例着色');
  assert.match(nodes.summary.textContent, /3 条流程 \/ 7 个任务/);
  assert.match(nodes.diagram.textContent, /F03-T02/);
  assert.match(nodes.diagram.textContent, /待估/);
  // 流程读数与任务进度共用服务端算好的同一分母，前端不参与任何换算
  assert.match(nodes.diagram.textContent, /1\/3 完成/);
  assert.match(nodes.diagram.textContent, /0\/2 完成/);
  assert.deepEqual(byClass(nodes.diagram, 'flow-progress').map(node => node.textContent),
    ['1/3 完成','0/2 完成','0/2 完成'], '流程读数直接取服务端的完成计数');
  // 前端不做业务计算：这几个函数一旦回来，就说明有人又把排程/进度搬进了浏览器
  for (const gone of ['computeSpans', 'taskProgress', 'ageOf'])
    assert.ok(!source.includes(gone), `${gone} 属于服务端的活，面板里不该再有第二份`);
  console.log('PASS: single execution queue / dashed non-dependency connectors / direct task.view.progress');

  // 面板只做只读展示：没有余额/结算/时价/Token，也没有磁盘条或新的外部请求入口
  assert.ok(!/¥|余额|代金券|结算|时价|AutoDL|Token/.test(nodes.summary.textContent + nodes.instances.textContent),
    '读数条与实例行只展示任务与实例状态');
  assert.ok([...descendants(nodes.diagram, 'a'), ...descendants(nodes.summary, 'a')]
    .every(link => link.href.startsWith('#task-')), '面板只提供任务内部引用，不提供 TensorBoard 直链');
  assert.ok(!/磁盘|disk_used|tf-logs|tensorboard/i.test(nodes.summary.textContent + nodes.instances.textContent),
    '面板不展示磁盘与 TensorBoard 信息（这些留给 agent 巡检）');
  assert.ok(calls.length > 0 && calls.every(call => call.url === '/api/plan' && call.options.method === undefined),
    '面板只发 GET /api/plan 读取请求');

  nodes.theme.fire('click');
  assert.equal(context.document.documentElement.dataset.theme, 'dark');
  nodes.theme.fire('click');
  assert.equal(context.document.documentElement.dataset.theme, 'light');
  console.log('PASS: theme toggle');

  const longTask = data.tasks[0];
  const savedLongTaskName = longTask.name;
  const longName = 'v7_no_feedback_no_relation_no_composition_no_structure消融实验完整任务名称';
  longTask.name = longName;
  await nodes.refresh.fire('click');
  const longNode = taskById(nodes.diagram, longTask.id);
  assert.equal(byClass(longNode, 'task-name')[0].textContent, longName, '长任务名称必须原样保留');
  assert.equal(byClass(longNode, 'task-id')[0].textContent, longTask.id, '名称换行不影响任务编号');
  assert.equal(byClass(longNode, 'status').length, 1, '名称换行不移除任务状态');
  longTask.name = savedLongTaskName;
  await nodes.refresh.fire('click');
  console.log('PASS: long task names remain complete with wrapping styles');

  // 任务进度必须直接取 task.view.progress；浏览器不读 graph，也不从依赖/时间重算。
  const progressTarget = data.tasks.find(task => task.id === 'F01-T03');
  const savedProgress = progressTarget.view.progress;
  const savedView = data.flows[0].view;
  progressTarget.view.progress = 40;
  data.flows[0].view = {total:7, countable:7, done:5, cancelled:2, open:2, progress:71,
    known:true, finish:data.tasks[0].estimate.finish};
  await nodes.refresh.fire('click');
  const shifted = taskById(nodes.diagram, progressTarget.id);
  assert.equal(byClass(shifted, 'task-meter-fill')[0].style, 'width:40%', '进度条宽度直接取服务端值');
  assert.equal(byClass(shifted, 'task-progress-value')[0].textContent, '40%');
  assert.match(nodes.diagram.textContent, /5\/7 完成 · 已取消 2/);
  assert.equal(byClass(nodes.diagram, 'flow-progress')[0].textContent, '5/7 完成 · 已取消 2');
  progressTarget.view.progress = savedProgress;
  data.flows[0].view = savedView;
  await nodes.refresh.fire('click');
  console.log('PASS: task progress rendered straight from task.view.progress');

  // 空队列 / 单任务 / 空车道：只消费服务端 lanes/other，不虚构任何图结构。
  const savedTasks = data.tasks;
  const savedQueues = data.queues;
  data.tasks = [];
  data.queues = {lanes:[],other:[]};
  await nodes.refresh.fire('click');
  assert.match(nodes.diagram.textContent, /暂无任务/);
  assert.equal(byClass(nodes.diagram, 'task-node').length, 0);
  const single = savedTasks.find(task => task.status === 'pending') || savedTasks[0];
  data.tasks = [single];
  data.queues = {lanes:[{instance:single.instance,tasks:[single.id]}],other:[]};
  await nodes.refresh.fire('click');
  assert.equal(byClass(nodes.diagram, 'task-node').length, 1, '单任务也照常渲染');
  assert.equal(byClass(nodes.diagram, 'queue-edge').length, 0, '单任务不画排队箭头');
  data.queues = {lanes:[{instance:'I01',tasks:[]}],other:[]};
  await nodes.refresh.fire('click');
  assert.match(nodes.diagram.textContent, /暂无执行中或待执行任务/, '空车道要给出空态');
  assert.equal(byClass(nodes.diagram, 'queue-edge').length, 0);
  data.tasks = savedTasks;
  data.queues = savedQueues;
  delete data.graph;
  await nodes.refresh.fire('click');
  assert.equal(byClass(nodes.diagram, 'task-node').length, 7, '快照没有 graph 也照样渲染执行队列');
  assert.equal(byClass(nodes.diagram, 'queue-edge').length, 2);
  assert.equal(byClass(nodes.diagram, 'canvas-edge').length, 0, '没有依赖图可画');
  assert.equal(byClass(nodes.diagram, 'diagram-view').length, 0, '没有视图切换入口');
  console.log('PASS: empty plan / singleton / empty lane / renders without graph');

  // 队列是原生滚动容器；重绘（10 秒轮询）后保留滚动位置
  const viewport = byClass(nodes.diagram, 'queue-viewport')[0];
  viewport.scrollLeft = 40; viewport.scrollTop = 30;
  viewport.fire('scroll',{});
  await nodes.refresh.fire('click');
  const viewportAfter = byClass(nodes.diagram, 'queue-viewport')[0];
  assert.equal(viewportAfter.scrollLeft, 40, '横向滚动位置必须在自动刷新后保留');
  assert.equal(viewportAfter.scrollTop, 30, '纵向滚动位置必须在自动刷新后保留');
  console.log('PASS: scrollable queue keeps its offset across refresh');

  // 整个流程都被取消（真实计划里常见）：只报这件事，不画进度读数，也不写"0/0 完成 · 完成待估"
  const savedSecond = data.flows[1].view;
  data.flows[1].view = {total:2, countable:0, done:0, cancelled:2, open:0, progress:null,
    known:true, finish:null};
  await nodes.refresh.fire('click');
  assert.match(nodes.diagram.textContent, /已全部取消（2 个任务）/);
  assert.equal(byClass(nodes.diagram, 'flow-progress').length, 2, '没有需要完成的任务时不画进度读数');
  assert.ok(!/0\/0 完成/.test(nodes.diagram.textContent), '不给"0/0 完成"这种空计数');
  data.flows[1].view = savedSecond;
  await nodes.refresh.fire('click');
  assert.equal(byClass(nodes.diagram, 'flow-progress').length, 3);
  // 空流程只是"暂无任务"，不能报成"已全部取消（0 个任务）"
  const savedEmptyView = data.flows[0].view;
  data.flows[0].view = {total:0, countable:0, done:0, cancelled:0, open:0, progress:null,
    known:true, finish:null};
  await nodes.refresh.fire('click');
  assert.match(byClass(nodes.diagram, 'flow-item')[0].textContent, /F01.*暂无任务/);
  assert.ok(!/已全部取消（0 个任务）/.test(nodes.diagram.textContent));
  data.flows[0].view = savedEmptyView;
  await nodes.refresh.fire('click');
  console.log('PASS: a fully cancelled flow reports just that, with no progress reading');

  // 逾期与巡检超时提示（先清干净，避免演示快照本身随时间变旧影响断言）
  data.tasks.forEach(task => { task.estimate.overdue = false; task.estimate.stale = false; });
  data.tasks[1].estimate.overdue = true;
  data.tasks[1].estimate.stale = true;
  await nodes.refresh.fire('click');
  assert.equal(byClass(nodes.diagram, 'eta-overdue').length, 1);
  assert.equal(byClass(nodes.diagram, 'eta-stale').length, 1);
  assert.match(nodes.diagram.textContent, /已超过预计完成时间，需重新估计/);
  assert.match(nodes.diagram.textContent, /超过 1 小时未巡检/);
  console.log('PASS: overdue and never-inspected running tasks are flagged');

  const malicious = '<img src=x onerror="alert(1)">';
  data.name = malicious; data.tasks[0].name = malicious; data.tasks[0].note = malicious;
  await nodes.refresh.fire('click');
  assert.equal(nodes.title.textContent, malicious);
  assert.equal(descendants(nodes.diagram, 'img').length, 0);
  assert.equal(byClass(taskById(nodes.diagram, data.tasks[0].id), 'task-name')[0].textContent, malicious);
  assert.match(nodes.diagram.textContent, /<img src=x/);
  console.log('PASS: untrusted plan text is not interpreted as HTML');

  const saved = JSON.parse(JSON.stringify(data));
  // 与 WWTP 同构：15 个无依赖实验，I01 七项，I02 八项（含执行中 v7）。
  const template = saved.tasks[0];
  data.tasks = Array.from({length:15}, (_,index) => ({...template,
    id:'Q' + (index + 1), name:'独立训练 ' + (index + 1), note:'', deps:[],
    instance:index < 7 ? 'I01' : 'I02', status:index === 7 ? 'running' : 'pending',
    view:{...template.view,progress:0},
    estimate:{...template.estimate,finish:null,reason:'预计耗时待估',overdue:false,stale:false}}));
  data.queues = {lanes:[{instance:'I01',tasks:data.tasks.slice(0,7).map(t => t.id)},
    {instance:'I02',tasks:data.tasks.slice(7).map(t => t.id)}],other:[]};
  vm.runInContext('renderQueues()',context);
  assert.equal(byClass(nodes.diagram,'task-node').length,15);
  assert.equal(byClass(nodes.diagram,'queue-edge').length,13,'两条实例队列共 13 条排队虚线');
  assert.equal(byClass(nodes.diagram,'canvas-edge').length,0,'不再有依赖图连线');
  assert.equal(new Set(byClass(nodes.diagram,'task-node').map(n => n.id)).size,15,'任务唯一');
  assert.ok(byClass(nodes.diagram,'queue-edge').every(edge => edge['aria-label'].includes('非硬依赖')));
  assert.match(html,/\.queue-edge\s*\{[^}]*dashed/);
  assert.ok(byClass(nodes.diagram,'queue-node').every(node =>
    byClass(node,'task-name').length === 1 && byClass(node,'task-meter').length === 1));
  assert.equal(byClass(nodes.diagram,'diagram-view').length,0,'没有视图切换入口');
  assert.equal(byClass(nodes.diagram,'task-node').length,15);
  data.queues = {lanes:[{instance:'I01',tasks:[]}],other:[{label:'本地任务',tasks:['Q1','Q2']}]};
  vm.runInContext('renderQueues()',context);
  assert.match(nodes.diagram.textContent,/暂无执行中或待执行任务/);
  assert.equal(byClass(nodes.diagram,'queue-edge').length,0,'本地、未分配或非执行任务不画排队箭头');
  data.tasks[0].name = malicious;
  vm.runInContext('renderQueues()',context);
  assert.equal(descendants(nodes.diagram,'img').length,0);
  assert.equal(byClass(taskById(nodes.diagram,'Q1'),'task-name')[0].textContent,malicious);
  Object.assign(data,saved);
  vm.runInContext('renderQueues()',context);
  console.log('PASS: independent tasks / instance queues / dashed non-dependency connectors / empty lanes / safe text');

  failed = true;
  await nodes.refresh.fire('click');
  assert.match(nodes.error.textContent, /offline/);
  assert.match(nodes.error.textContent, /上次读取的数据/);
  assert.equal(nodes.error.classList.contains('hidden'), false);
  assert.equal(nodes.refresh.disabled, false);
  assert.equal(byClass(nodes.diagram, 'task-node').length, 7);
  assert.ok(calls.every(call => call.url === '/api/plan' && call.options.method === undefined),
    '整轮交互中面板始终只发 GET /api/plan，没有写请求');
  console.log('PASS: offline warning / last snapshot retained / controls recovered');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
