// Dependency-free DOM smoke tests for the task flowchart: the server computes grid
// columns/rows and cross-layer waypoints; the panel renders one scrollable canvas.
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
// 凭据只从面板登记进服务端内存：子进程读不到任何 token，这里不必再"指开"凭据文件。
execFileSync('python3', [cli, '--project', project, 'demo'], {encoding: 'utf8'});
const data = JSON.parse(execFileSync('python3', [cli, '--project', project, 'show', '--json'],
  {encoding: 'utf8'}));
const nodes = Object.fromEntries(['title','updated','instances','summary','diagram','error','refresh','theme','token','modal']
  .map(id => [id, new Element('div')]));
let failed = false;
const calls = [];              // 面板发出的每个请求，用来断言时价登记真的走了 POST
let postResult = null;         // 覆盖下一次 POST 的响应，用来演服务端拒绝
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
    if (options.method === 'POST' && postResult) return postResult;
    return {ok:true,json:async () => data};
  },
  AbortController, Date, console,
  setTimeout: () => 1, clearTimeout: () => {}, setInterval: () => 1,
});

async function main() {
  assert.ok(!html.includes('id="search"'), 'Task search input should be removed');
  assert.ok(!html.includes('<details'), 'Task details dropdown should be removed');
  // DOM 桩不做排版：同时锁定完整名称所需的 CSS，避免文本在 DOM 中完整但仍被省略。
  const taskNameCSS = html.match(/\.task-name\s*\{([^}]+)\}/)[1];
  assert.match(taskNameCSS, /white-space:\s*normal/, '任务名称必须允许换行');
  assert.match(taskNameCSS, /overflow-wrap:\s*anywhere/, '连续英文/下划线名称也必须能换行');
  assert.ok(!/text-overflow:\s*ellipsis|overflow:\s*hidden/.test(taskNameCSS), '任务名称不能裁剪或省略');
  const taskTopCSS = html.match(/\.task-top\s*\{([^}]+)\}/)[1];
  assert.match(taskTopCSS, /flex-wrap:\s*wrap/, '窄屏状态必须能换行，不能挤掉名称');
  // DOM 桩看不到层叠：id 选择器的 display:flex 会压过 .hidden，空弹窗就成了全屏遮罩
  assert.ok(html.includes('#modal.hidden'), '关闭态必须有 #modal.hidden 压过 #modal 的 display:flex');
  // 同理：.notice 与 .hidden 同为一条类选择器，靠后写的赢，空错误条会整条盖在读数条上方
  assert.ok(html.includes('.notice.hidden'), '关闭态必须有 .notice.hidden 压过 .notice 的 display:flex');
  new vm.Script(source, {filename:'index.html'}).runInContext(context);
  await new Promise(resolve => setImmediate(resolve));
  const canvases = byClass(nodes.diagram, 'canvas');
  assert.equal(canvases.length, 1, '整份计划只能有一张主图（一块画布）');
  assert.equal(byClass(nodes.diagram, 'canvas-viewport').length, 1, '画布要有可滚动视口');
  assert.equal(byClass(nodes.diagram, 'start-node').length, 0, '不显示开始节点');
  assert.ok(!nodes.diagram.textContent.includes('开始'), '画布上不出现开始节点文案');
  const taskNodes = byClass(nodes.diagram, 'task-node');
  assert.equal(taskNodes.length, 7, 'All tasks appear exactly once');
  assert.equal(descendants(nodes.diagram, 'details').length, 0);
  assert.ok(taskNodes.every(node => byClass(node, 'task-node').length === 0),
    '每个任务节点都是叶子，不包含子任务');
  assert.ok(taskNodes.every(node => descendants(node, 'a').length === 0),
    '跨流程依赖由主图箭头表达，不再在任务节点里塞外部引用列表');
  assert.ok(!nodes.diagram.textContent.includes('前置（全部完成）'), '任务节点不再列出子任务/前置清单');
  assert.ok(taskNodes.every(node => node.classList.contains('canvas-node')), '任务节点按网格绝对定位');
  const expectedEdges = data.graph.edges;
  const edges = byClass(nodes.diagram, 'canvas-edge');
  assert.equal(edges.length, expectedEdges.length + expectedEdges.reduce((n, edge) => n + edge.waypoints.length, 0),
    '每条依赖按相邻层分段绘制');
  assert.deepEqual(edges.map(edge => [edge.dataset.source,edge.dataset.target]),
    expectedEdges.map(edge => [edge.source,edge.target]));
  assert.ok(!expectedEdges.some(edge => edge.source === 'START'), '不带任何开始节点边');
  assert.equal(byClass(nodes.diagram, 'canvas-edge')
    .filter(edge => edge.classList.contains('arrow')).length, expectedEdges.length,
    '每条依赖只在最后一段画箭头');
  const nodeCSS = html.match(/\.task-node\s*\{([^}]+)\}/)[1];
  assert.ok(!/(?:^|;)\s*(?:max-)?height\s*:/.test(nodeCSS), '节点随正文变高，不能固定或限制高度');
  assert.match(nodeCSS, /border-left-width:\s*6px/, '任务节点色带要足够醒目');
  assert.match(html.match(/\.instance\s*\{([^}]+)\}/)[1], /border-left:6px solid var\(--inst-color/,
    '实例行色带与任务节点同宽');
  assert.match(html, /\.canvas-viewport\s*\{[^}]*overflow:auto/, '画布视口用原生滚动查看大图');
  assert.ok(!/\.canvas-viewport\s*\{[^}]*cursor:grab/.test(html), '不再用自定义拖拽平移');
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
  assert.match(html, /\.canvas-edge\s*\{[^}]*transform-origin:0 0/, '连线按端点坐标旋转绘制');
  // 节点按服务端 layer/row 映射到网格：演示数据中 F01-T01 在 (0,0)，F01-T02 在 (1,0)，F02-T01 在 (1,1)
  assert.equal(taskNodes[0].style, 'left:64px;top:56px', '第一个节点落在网格首格');
  assert.equal(taskNodes[1].style, 'left:444px;top:56px', '同层节点共享列坐标');
  assert.equal(taskNodes[2].style, 'left:444px;top:266px', '不同行按 ROW_PITCH 排布');
  assert.equal(canvases[0].style, 'width:1928px;height:548px',
    '画布尺寸由服务端行列和固定网格间距推出');
  assert.equal(edges[0].classList.contains('arrow'), true);
  assert.ok(edges.every(edge => !edge.className.includes('inst-')),
    '连线仍为中性色，不按实例着色');
  assert.equal(byClass(nodes.diagram, 'instance-tag').length, 7, '每个任务节点都标出所在实例');
  assert.equal(byClass(nodes.diagram, 'legend-item').length, 0, '不画实例颜色图例');
  for (const forbidden of ['getBoundingClientRect', 'ResizeObserver', 'requestAnimationFrame',
    'infinite', '<canvas', 'gantt-', 'laneLeft', 'innerHTML'])
    assert.ok(!html.includes(forbidden), `流程图不能包含 ${forbidden}`);
  assert.ok(!/<(?:script|link)[^>]+(?:src|href)=["']https?:/.test(html), 'No external assets');
  const values = byClass(nodes.diagram, 'task-progress-value');
  assert.equal(values.length, 7);
  assert.ok(values.some(node => node.textContent === '18%'));
  assert.equal(byClass(nodes.diagram, 'task-meter-fill').length, 7);
  assert.equal(byClass(nodes.diagram, 'flow-item').length, 3, '每条流程一条纯文本读数');
  assert.equal(descendants(nodes.diagram, 'a').length, 0, '单主图不需要跨流程跳转链接');
  assert.equal(nodes.instances.children.length, 2);
  const instanceRows = byClass(nodes.instances, 'instance');
  assert.deepEqual(instanceRows.map(row => [...row.classes].find(name => name.startsWith('inst-color-'))),
    ['inst-color-0','inst-color-1'], '实例行标出身份色');
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
  console.log('PASS: single scrollable canvas / grid nodes / no start node / non-crossing routes / instance-colored running nodes');

  // 面板唯一的写入口：实例行上的时价按钮 → 弹窗 → POST /api/instance-price
  const priceButtons = byClass(nodes.instances, 'price');
  assert.equal(priceButtons.length, 2, '每台实例行上都要有登记时价的入口');
  assert.equal(priceButtons[0].textContent, '¥1.88/h', '已登记的时价直接显示在卡上');
  priceButtons[0].fire('click');
  const dialogNode = () => byClass(nodes.modal, 'dialog')[0];
  const dialogInput = () => descendants(nodes.modal, 'input')[0];
  const dialogButton = text => descendants(nodes.modal, 'button').find(button => button.textContent === text);
  assert.equal(nodes.modal.classList.contains('hidden'), false, '点时价按钮要打开弹窗');
  assert.match(dialogNode().textContent, /登记 GPU 时价 · I01 基线 \/ GPU A/);
  assert.match(dialogNode().textContent, /不会刷新实例的观测时间/);
  assert.equal(dialogInput().value, '1.88', '弹窗要预填当前时价，不能让人重新查一遍');
  dialogInput().value = '2.5';
  dialogButton('保存').fire('click');
  await new Promise(resolve => setImmediate(resolve));
  const posted = calls.filter(call => call.options.method === 'POST').pop();
  assert.equal(posted.url, '/api/instance-price');
  assert.equal(posted.options.headers['Content-Type'], 'application/json');
  assert.equal(posted.options.body, '{"id":"I01","price":2.5}');
  assert.equal(nodes.modal.classList.contains('hidden'), true, '登记成功后关窗');
  assert.equal(nodes.modal.children.length, 0);

  // 服务端拒绝时错误留在弹窗里：不关窗、不改本地状态、不假装成功
  postResult = {ok:false, json: async () => ({error:'时价 必须在 0.001 到 10000 之间且为有限数字'})};
  byClass(nodes.instances, 'price')[0].fire('click');
  dialogInput().value = '0';
  dialogButton('保存').fire('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(dialogNode().textContent, /登记失败：时价 必须在 0\.001 到 10000 之间且为有限数字/);
  assert.equal(nodes.modal.classList.contains('hidden'), false, '被拒绝时不能关窗');
  // 清空发的是 null；缺时价就少给结论，绝不补一个 0
  postResult = null;
  dialogButton('清空时价').fire('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.filter(call => call.options.method === 'POST').pop().options.body, '{"id":"I01","price":null}');
  // 空输入只在弹窗里提示，不发请求：不登记时价的结果是"少给结论"，不是报错
  byClass(nodes.instances, 'price')[0].fire('click');
  dialogInput().value = '   ';
  const sent = calls.length;
  dialogButton('保存').fire('click');
  assert.equal(calls.length, sent, '空输入不该发请求');
  assert.match(dialogNode().textContent, /还没填单价/);
  assert.equal(byClass(nodes.modal, 'dialog-error').length, 0, '没填不是错误，用中性提示就行');
  // 弹窗里的实例名同样只走 textContent
  const hostileName = '<img src=x onerror="alert(1)">';
  const savedName = data.instances[0].name;
  data.instances[0].name = hostileName;
  byClass(nodes.instances, 'price')[0].fire('click');
  assert.equal(descendants(nodes.modal, 'img').length, 0, '弹窗标题不能被当成 HTML');
  assert.match(dialogNode().textContent, /<img src=x/);
  data.instances[0].name = savedName;
  dialogButton('取消').fire('click');
  assert.equal(nodes.modal.children.length, 0, '取消后弹窗清空');
  assert.equal(nodes.modal.classList.contains('hidden'), true);
  console.log('PASS: GPU price is registered from the panel and rejections stay in the dialog');

  // 无卡开机（含持续请求等待 GPU）：没有 GPU 在计费，弹窗改成登记无卡费率，不碰 GPU 时价
  data.instances[1].mode = 'no-gpu';
  data.instances[1].no_gpu_price = 0.1;
  await nodes.refresh.fire('click');
  const noGpuButton = byClass(nodes.instances, 'price')[1];
  assert.equal(noGpuButton.textContent, '无卡 ¥0.1/h', '无卡实例显示的是无卡费率，不是 GPU 时价');
  noGpuButton.fire('click');
  assert.match(dialogNode().textContent, /登记无卡费率 · I02/);
  assert.match(dialogNode().textContent, /GPU 模式开机后再登记 GPU 时价/);
  assert.equal(dialogInput().value, '0.1', '无卡档位要预填当前无卡费率');
  dialogInput().value = '0.12';
  dialogButton('保存').fire('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.filter(call => call.options.method === 'POST').pop().options.body,
    '{"id":"I02","no_gpu_price":0.12}', '无卡费率写 no_gpu_price，不能把 GPU 时价覆盖掉');
  assert.equal(nodes.modal.classList.contains('hidden'), true, '登记成功后关窗');
  // 开机模式没登记就退回 GPU 时价：没有依据时宁可高估花费
  data.instances[1].mode = null;
  await nodes.refresh.fire('click');
  assert.equal(byClass(nodes.instances, 'price')[1].textContent, '¥1.58/h');
  console.log('PASS: no-GPU instances register the no-GPU rate instead of a GPU price');

  // Token 只从面板登记、只进服务端内存：请求形状、状态文案，客户端一律不留副本
  assert.ok(html.includes('Token 只存本进程内存'), '页脚要说清 Token 存在哪，免得人去找凭据文件');
  data.credentials = {configured:false,set_at:null};
  await nodes.refresh.fire('click');
  assert.ok(nodes.token.classList.contains('unset'), '没登记 Token 时按钮要转警示态');
  nodes.token.fire('click');
  assert.match(dialogNode().textContent, /登记开发者 Token/);
  assert.match(dialogNode().textContent, /只存在这个面板进程的内存里/);
  assert.equal(dialogInput().type, 'password', 'Token 不能在屏幕上回显');
  assert.equal(dialogInput().value, '', '弹窗不回填：客户端不留副本');
  const beforeToken = calls.length;
  dialogButton('保存').fire('click');
  assert.equal(calls.length, beforeToken, '空输入不该发请求');
  assert.match(dialogNode().textContent, /还没填 Token/);
  postResult = {ok:true, json: async () => ({...data, credentials:{configured:true,set_at:'2026-10-15T09:00:00+00:00'}})};
  dialogInput().value = 'secret-token-value';
  dialogButton('保存').fire('click');
  await new Promise(resolve => setImmediate(resolve));
  const tokenPost = calls.filter(call => call.options.method === 'POST').pop();
  assert.equal(tokenPost.url, '/api/token');
  assert.equal(tokenPost.options.body, '{"token":"secret-token-value"}');
  assert.equal(nodes.modal.classList.contains('hidden'), true, '登记成功后关窗');
  assert.equal(nodes.modal.children.length, 0, '关窗后输入框连同里面的值一起消失');
  assert.ok(!nodes.token.classList.contains('unset'), '登记后按钮回到常态');
  const page = [nodes.title,nodes.updated,nodes.summary,nodes.instances,nodes.diagram,nodes.error,nodes.modal];
  assert.ok(!page.some(node => node.textContent.includes('secret-token-value')),
    '凭据不能留在页面任何位置');
  // 被拒绝时留在弹窗里：这是服务端说的实话，不是客户端编的
  postResult = {ok:false, json: async () => ({error:'Token 超过 512 字符，请核对是否粘错了内容'})};
  nodes.token.fire('click');
  dialogInput().value = 'x'.repeat(600);
  dialogButton('保存').fire('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(dialogNode().textContent, /登记失败：Token 超过 512 字符/);
  assert.equal(nodes.modal.classList.contains('hidden'), false, '被拒绝时不能关窗');
  postResult = null;
  dialogButton('清除 Token').fire('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.filter(call => call.options.method === 'POST').pop().options.body, '{"token":null}',
    '清除要发 null，不是空串');
  console.log('PASS: developer token is registered from the panel and never kept client-side');

  // 余额只有一块：金额、代金券标注、按当前开机时价的可跑时长、计划花费与充足结论
  const budgetChips = byClass(nodes.summary, 'budget');
  assert.equal(budgetChips.length, 1, '余额块必须渲染且只有一块');
  assert.match(budgetChips[0].textContent, /余额 ¥128\.50/);
  assert.match(budgetChips[0].textContent, /观测于 (刚刚|\d+ (分钟|小时|天)前)/);
  assert.match(nodes.summary.textContent, /代金券 ¥20\.00（不计入可用时长）/);
  assert.match(nodes.summary.textContent, /现价可跑 37\.1 小时/);
  // 计划剩余时长随真实时间推移而变化，这里只要求它被算出来并带上预估花费
  assert.match(nodes.summary.textContent, /跑完剩余计划 \d+(\.\d+)? GPU 小时≈ ¥\d+\.\d\d/);
  assert.equal(byClass(nodes.summary, 'good').length, 1, '余额充足时给出明确的结论');
  console.log('PASS: balance and affordable GPU hours are shown in the summary');

  // 自动结算记录：整句文案由服务端写进计划，面板只显示、只按 over 换样式，不在浏览器里重算
  assert.ok(data.settlement && typeof data.settlement.text === 'string',
    '/api/plan 必须带上服务端算好的结算视图');
  assert.equal(byClass(nodes.summary, 'settled').length, 1, '计划里有结算记录就要渲染一块结算徽标');
  assert.match(byClass(nodes.summary, 'settled')[0].textContent, /自动结算/);
  data.settlement = {live:false, over:false, computed_at:new Date().toISOString(),
    text:'自动结算于 3 分钟前 · 跑完剩余计划 4.8 GPU 小时、约 ¥9.62 · 余额够'};
  await nodes.refresh.fire('click');
  assert.equal(byClass(nodes.summary, 'bad').length, 0, '结论正常时结算记录不该报警');
  assert.match(nodes.summary.textContent, /自动结算于 3 分钟前 · 跑完剩余计划 4\.8 GPU 小时、约 ¥9\.62 · 余额够/);
  data.settlement = {live:true, over:true, text:'自动结算已过期（3 小时前，超过 2 个周期）· 当前为实时值 · 上次结算失败'};
  await nodes.refresh.fire('click');
  assert.ok(byClass(nodes.summary, 'settled')[0].classList.contains('bad'), '结算过期/失败时必须用醒目样式');
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
  assert.ok([...descendants(nodes.diagram, 'a'), ...descendants(nodes.summary, 'a')]
    .every(link => link.href.startsWith('#task-')), '面板只提供任务内部引用，不提供 TensorBoard 直链');
  assert.ok(!/磁盘|disk_used|tf-logs|tensorboard/i.test(nodes.summary.textContent + nodes.instances.textContent),
    '面板不展示磁盘与 TensorBoard 信息（这些留给 agent 巡检）');

  nodes.theme.fire('click');
  assert.equal(context.document.documentElement.dataset.theme, 'dark');
  nodes.theme.fire('click');
  assert.equal(context.document.documentElement.dataset.theme, 'light');
  console.log('PASS: theme toggle');

  const savedLongTaskName = data.tasks[0].name;
  const longName = 'v7_no_feedback_no_relation_no_composition_no_structure消融实验完整任务名称';
  data.tasks[0].name = longName;
  await nodes.refresh.fire('click');
  const longTitle = byClass(nodes.diagram, 'task-title')[0];
  assert.equal(byClass(longTitle, 'task-name')[0].textContent, longName, '长任务名称必须原样保留');
  assert.equal(byClass(longTitle, 'task-id')[0].textContent, data.tasks[0].id, '名称换行不影响任务编号');
  assert.equal(byClass(byClass(nodes.diagram, 'task-node')[0], 'status').length, 1, '名称换行不移除任务状态');
  data.tasks[0].name = savedLongTaskName;
  await nodes.refresh.fire('click');
  console.log('PASS: long task names remain complete with wrapping styles');

  // 服务端的节点行列和进度必须直传；浏览器不从依赖或时间重新布局。
  const savedNode = data.graph.nodes[2];
  const savedView = data.flows[0].view;
  data.graph.nodes[2] = {...savedNode, row:1, layer:2, progress:40};
  data.flows[0].view = {total:7, countable:7, done:5, cancelled:2, open:2, progress:71,
    known:true, finish:data.tasks[0].estimate.finish};
  await nodes.refresh.fire('click');
  const shifted = byClass(nodes.diagram, 'task-node')[2];
  assert.equal(shifted.style, 'left:824px;top:266px', '节点位置直接取服务端 layer/row');
  assert.equal(byClass(shifted, 'task-meter-fill')[0].style, 'width:40%');
  assert.equal(byClass(shifted, 'task-progress-value')[0].textContent, '40%');
  assert.match(nodes.diagram.textContent, /5\/7 完成 · 已取消 2/);
  assert.equal(byClass(nodes.diagram, 'flow-progress')[0].textContent, '5/7 完成 · 已取消 2');
  data.graph.nodes[2] = savedNode;
  data.flows[0].view = savedView;
  await nodes.refresh.fire('click');
  console.log('PASS: node grid positions and progress rendered straight from the server payload');

  // 空图、单节点、分叉汇合直接消费服务端拓扑，不虚构判断菱形。
  const savedGraph = data.graph;
  data.graph = {nodes:[], edges:[], columns:0, rows:0};
  await nodes.refresh.fire('click');
  assert.match(nodes.diagram.textContent, /暂无任务/);
  assert.equal(byClass(nodes.diagram, 'canvas').length, 0);
  const firstTask = savedGraph.nodes[0];
  data.graph = {nodes:[{...firstTask, row:0, layer:0}], edges:[], columns:1, rows:1};
  await nodes.refresh.fire('click');
  assert.equal(byClass(nodes.diagram, 'canvas').length, 1, '有任务时始终只有一块画布');
  assert.equal(byClass(byClass(nodes.diagram, 'canvas')[0], 'task-node').length, 1);
  assert.equal(byClass(nodes.diagram, 'start-node').length, 0, '单节点图也不画开始节点');
  const ids = data.tasks.filter(task => task.flow === data.flows[0].id).map(task => task.id);
  data.graph = {
    nodes:[{id:ids[0],row:0,layer:0,progress:0},
      {id:ids[1],row:0,layer:1,progress:0},
      {id:ids[2],row:1,layer:1,progress:0}],
    edges:[{source:ids[0],target:ids[1],waypoints:[]},
      {source:ids[0],target:ids[2],waypoints:[]}],
    columns:2, rows:2};
  await nodes.refresh.fire('click');
  const branched = byClass(nodes.diagram, 'canvas')[0];
  assert.equal(byClass(branched, 'canvas-edge').length, 2);
  assert.equal(byClass(branched, 'canvas-edge')
    .filter(edge => edge.classList.contains('arrow')).length, 2);
  assert.equal(byClass(branched, 'canvas-edge')[0].style,
    'left:344px;top:136px;width:100px;transform:rotate(0rad)',
    '同排依赖画成水平箭头');
  assert.equal(byClass(branched, 'canvas-edge')[1].style,
    'left:344px;top:136px;width:' + Math.hypot(100,210)
      + 'px;transform:rotate(' + Math.atan2(210,100) + 'rad)',
    '并行分支画成直线分叉，不绕行到画布边缘');
  data.graph = savedGraph;
  await nodes.refresh.fire('click');
  // 跨层依赖：斜线只走列间空隙，路点行内水平穿过空单元格，最后一段才带箭头
  data.graph = {
    nodes:[{id:ids[0],row:0,layer:0,progress:0},
      {id:ids[2],row:0,layer:2,progress:0}],
    edges:[{source:ids[0],target:ids[2],waypoints:[{layer:1,row:1}]}],
    columns:3, rows:2};
  await nodes.refresh.fire('click');
  const routed = byClass(nodes.diagram, 'canvas-edge');
  assert.equal(routed.length, 3, '跨层边拆成两条斜线 + 一条空行水平线');
  assert.equal(routed.filter(edge => edge.classList.contains('arrow')).length, 1,
    '跨层边只在最后一段画箭头');
  assert.equal(routed[0].style,
    'left:344px;top:136px;width:' + Math.hypot(100,210)
      + 'px;transform:rotate(' + Math.atan2(210,100) + 'rad)');
  assert.equal(routed[1].style, 'left:444px;top:346px;width:280px;transform:rotate(0rad)',
    '路点行内的水平线穿过没有节点的空单元格');
  assert.equal(routed[2].style,
    'left:724px;top:346px;width:' + Math.hypot(100,210)
      + 'px;transform:rotate(' + Math.atan2(-210,100) + 'rad)');
  data.graph = savedGraph;
  await nodes.refresh.fire('click');
  console.log('PASS: empty / singleton / fork / cross-layer routing with straight canvas arrows');

  // 画布是原生滚动容器；重绘（10 秒轮询）后保留滚动位置
  const viewport = byClass(nodes.diagram, 'canvas-viewport')[0];
  viewport.scrollLeft = 40; viewport.scrollTop = 30;
  viewport.fire('scroll',{});
  await nodes.refresh.fire('click');
  const viewportAfter = byClass(nodes.diagram, 'canvas-viewport')[0];
  assert.equal(viewportAfter.scrollLeft, 40, '横向滚动位置必须在自动刷新后保留');
  assert.equal(viewportAfter.scrollTop, 30, '纵向滚动位置必须在自动刷新后保留');
  console.log('PASS: scrollable canvas keeps its offset across refresh');

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
  assert.equal(byClass(nodes.diagram, 'task-name')[0].textContent, malicious);
  assert.match(nodes.diagram.textContent, /<img src=x/);
  console.log('PASS: untrusted plan text is not interpreted as HTML');

  failed = true;
  await nodes.refresh.fire('click');
  assert.match(nodes.error.textContent, /offline/);
  assert.match(nodes.error.textContent, /上次读取的数据/);
  assert.equal(nodes.error.classList.contains('hidden'), false);
  assert.equal(nodes.refresh.disabled, false);
  assert.equal(byClass(nodes.diagram, 'task-node').length, 7);
  console.log('PASS: offline warning / last snapshot retained / controls recovered');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
