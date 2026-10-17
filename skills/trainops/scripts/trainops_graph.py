"""排程、依赖图、余额与结算这些派生计算。

所有函数都只读计划，不写文件；面板、CLI 与结算线程共用同一份口径。
"""

import bisect
from datetime import timedelta
import hashlib
import heapq
import json
import math

from trainops_base import *
from trainops_model import instance_rate, validate


def forecast(plan, now=None):
    """Deterministic dependency-ready scheduling with one lane per GPU instance."""
    now = now or utcnow()
    tasks = {t["id"]: t for t in plan["tasks"]}
    order = {key: natural_id(key) for key in tasks}
    results, lanes = {}, {}
    active = {t["instance"]: t["id"] for t in tasks.values()
              if t["status"] == "running" and t["instance"] != "local"}

    def record(task, start=None, end=None, reason="", overdue=False):
        # stale 只对执行中的任务有意义：其余状态没有"需要重新巡检"的概念。
        results[task["id"]] = {"start": stamp(start) if start else None,
                               "finish": stamp(end) if end else None,
                               "reason": reason, "overdue": overdue, "ready": False,
                               "stale": task["status"] == "running" and (
                                   not task["checked_at"]
                                   or (now - date(task["checked_at"])).total_seconds() > 3600)}
        return end

    for task in sorted(tasks.values(), key=lambda t: order[t["id"]]):
        status = task["status"]
        if status == "done":
            record(task, date(task["started_at"]), date(task["finished_at"]), "实际完成")
        elif status in ("failed", "cancelled", "blocked"):
            record(task, date(task["started_at"]) if task["started_at"] else None,
                   reason={"failed": "失败，等待处理", "cancelled": "已取消，不满足下游依赖",
                           "blocked": "人工标记阻塞"}[status])
        elif status == "running":
            start, end = date(task["started_at"]), None
            if task["duration_minutes"] is not None:
                end = start + timedelta(minutes=task["duration_minutes"])
            progress = task["progress"]
            if progress is not None and 0 < progress < 100 and task["progress_at"]:
                observed = date(task["progress_at"])
                if observed > start:
                    end = start + (observed - start) * (100 / progress)
            overdue = end is not None and end <= now
            if progress == 100:
                end = None
                reason = "进度 100%，待核实进程退出与产物"
            elif overdue:
                end = None
                reason = "已超过估计，需重估"
            else:
                reason = "按进度/耗时估计" if end else "运行中，待估耗时"
            record(task, start, end, reason, overdue)
            if task["instance"] != "local":
                lanes[task["instance"]] = end

    pending = {key for key in tasks if key not in results}
    while pending:
        candidates = []
        for key in sorted(pending, key=order.__getitem__):
            task = tasks[key]
            if not all(dep in results for dep in task["deps"]):
                continue
            unknown = [dep for dep in task["deps"] if results[dep]["finish"] is None]
            if unknown:
                candidates.append((now, order[key], key, "依赖待完成/待估: " + ", ".join(unknown)))
                continue
            earliest = max([now] + [date(results[dep]["finish"]) for dep in task["deps"]])
            if task["not_before"]:
                earliest = max(earliest, date(task["not_before"]))
            resource = task["instance"]
            if resource is None:
                candidates.append((earliest, order[key], key, "未分配实例（本地任务设 local）"))
            elif resource in lanes and lanes[resource] is None:
                candidates.append((earliest, order[key], key, "实例前序任务完成时间待估"))
            else:
                earliest = max(earliest, lanes.get(resource) or now)
                candidates.append((earliest, order[key], key, ""))
        if not candidates:
            raise PlanError("依赖无法排程，请检查环或引用")
        start, _, key, reason = min(candidates)
        task = tasks[key]
        resource = task["instance"]
        if reason:
            record(task, reason=reason)
        else:
            duration = task["duration_minutes"]
            end = start + timedelta(minutes=duration) if duration is not None else None
            record(task, start, end, "" if end else "预计耗时待估")
            results[key]["ready"] = (all(tasks[d]["status"] == "done" for d in task["deps"])
                                      and (not task["not_before"] or date(task["not_before"]) <= now)
                                      and resource not in active)
            if resource != "local":
                lanes[resource] = end
        pending.remove(key)
    return results


def rounded(value):
    """四舍五入取整：面板要的是整数百分比，这里统一口径，别让各处各写一套。"""
    return int(math.floor(value + 0.5))


def node_progress(task):
    """节点进度：done 一律满格，其余取登记过的真实进度；没进度就是 0，不猜。"""
    if task["status"] == "done":
        return 100
    if task["progress"] is None:
        return 0
    return max(0, min(100, rounded(task["progress"])))


def layout(plan, now=None, estimates=None):
    """把所有流程的任务排成一张尽量少交叉的有向无环图，并给出各流程的完成结论。

    整份计划只有一个主图：跨流程依赖与流程内依赖一样，都是图中的一条边。
    节点按层（列）与行排成网格；跨层依赖在中间层插入不可见的走线节点。
    排序用重心法 + 相邻交换：每次只计算受影响的两层局部交叉数，
    不再为每个候选交换重算整张图。每列内未完成任务占上半部，已完成节点下沉；
    完成但仍被未完成任务直接依赖的前置节点留在中间，更早的上游随链一起下沉。
    浏览器只按行列摆节点、按路点画箭头。
    """
    now = now or utcnow()
    estimates = forecast(plan, now) if estimates is None else estimates
    tasks_by_id = {task["id"]: task for task in plan["tasks"]}
    order = {key: natural_id(key) for key in tasks_by_id}
    grouped = {flow["id"]: [] for flow in plan["flows"]}
    views = {}
    ordered = sorted(plan["tasks"], key=lambda task: order[task["id"]])
    for task in ordered:
        grouped[task["flow"]].append(task)
    for flow in plan["flows"]:
        tasks = grouped[flow["id"]]
        countable = [task for task in tasks if task["status"] != "cancelled"]
        done = [task for task in tasks if task["status"] == "done"]
        open_tasks = [task for task in tasks if task["status"] not in TERMINAL]
        finishes = [date(estimates[task["id"]]["finish"]) for task in tasks
                    if estimates[task["id"]]["finish"]]
        views[flow["id"]] = {
            "total": len(tasks), "countable": len(countable), "done": len(done),
            "cancelled": len(tasks) - len(countable), "open": len(open_tasks),
            # 取消不计入分母；全取消时无进度结论。known 只检查尚未结束的任务。
            "progress": rounded(100 * len(done) / len(countable)) if countable else None,
            "known": all(estimates[task["id"]]["finish"] for task in open_tasks),
            "finish": stamp(max(finishes)) if finishes else None,
        }
    if not tasks_by_id:
        return {"nodes": [], "edges": [], "columns": 0, "rows": 0}, views

    children = {key: [] for key in tasks_by_id}
    degrees = {key: len(task["deps"]) for key, task in tasks_by_id.items()}
    layers = {key: 0 for key in tasks_by_id}
    for task in ordered:
        for dep in task["deps"]:
            children[dep].append(task["id"])
    ready = [(order[key], key) for key, count in degrees.items() if count == 0]
    heapq.heapify(ready)
    while ready:
        _, key = heapq.heappop(ready)
        for child in children[key]:
            layers[child] = max(layers[child], layers[key] + 1)
            degrees[child] -= 1
            if degrees[child] == 0:
                heapq.heappush(ready, (order[child], child))
    columns = max(layers.values()) + 1

    # 行优先级：未完成任务置顶；完成但仍被未完成任务直接依赖的前置节点居中；
    # 其余已完成节点（含更早的上游）沉底。行只表达可读性，不表达时间或资源。
    open_tasks = {key for key, task in tasks_by_id.items()
                  if task["status"] not in ("done", "cancelled")}
    needed = {dep for key in open_tasks for dep in tasks_by_id[key]["deps"]}
    row_group = {key: 0 if key in open_tasks else (1 if key in needed else 2)
                 for key in tasks_by_id}

    # 每条依赖一条边；跨层的边在中间层各插入一个不可见路点，保证每条线段只连接相邻两层。
    edges = []
    for task in ordered:
        for dep in sorted(task["deps"], key=order.__getitem__):
            edges.append((dep, task["id"]))
    per_layer = [[] for _ in range(columns)]
    for task in tasks_by_id:
        per_layer[layers[task]].append(task)
    dummy_keys = {}
    chains = []
    for index, (source, target) in enumerate(edges):
        chain = [source]
        for layer in range(layers[source] + 1, layers[target]):
            node = f"@{index}:{layer}"
            dummy_keys[node] = (index, layer)
            chain.append(node)
            per_layer[layer].append(node)
        chain.append(target)
        chains.append(chain)

    def node_key(node):
        if node in tasks_by_id:
            return (0,) + order[node]
        index, layer = dummy_keys[node]
        return (1, index, layer)

    def node_group(node):
        """路点跟随它要通往的任务：未完成目标的连线同样留在上半部。"""
        if node in row_group:
            return row_group[node]
        index, _ = dummy_keys[node]
        return row_group[edges[index][1]]

    for layer in per_layer:
        layer.sort(key=lambda node: (node_group(node), node_key(node)))

    # 相邻层连线先按列归组，数交叉时只需要扫这一列的线段。
    nexts, prevs = {}, {}
    segments = [[] for _ in range(columns - 1)]
    for chain in chains:
        for source, target in zip(chain, chain[1:]):
            nexts.setdefault(source, []).append(target)
            prevs.setdefault(target, []).append(source)
            column = dummy_keys[source][1] if source in dummy_keys else layers[source]
            segments[column].append((source, target))

    def positions():
        return [{node: index for index, node in enumerate(layer)} for layer in per_layer]

    def order_layer(layer, neighbor_positions, neighbors):
        base = {node: index for index, node in enumerate(layer)}

        def key(node):
            values = [neighbor_positions[item] for item in neighbors.get(node, [])]
            center = sum(values) / len(values) if values else base[node]
            return node_group(node), center, base[node], node_key(node)

        layer.sort(key=key)

    # 重心排序：向下、向上各两轮。只排序，不做全图交叉计数。
    for _ in range(4):
        for column in range(1, columns):
            order_layer(per_layer[column],
                        {node: index for index, node in enumerate(per_layer[column - 1])}, prevs)
        for column in range(columns - 2, -1, -1):
            order_layer(per_layer[column],
                        {node: index for index, node in enumerate(per_layer[column + 1])}, nexts)
    pos = positions()

    def crossing_count():
        """相邻层线段交叉数；同一源或同一目标的两段不算交叉。"""
        total = 0
        for column, group in enumerate(segments):
            if not group:
                continue
            left, right = pos[column], pos[column + 1]
            size = len(per_layer[column + 1])
            tree = [0] * (size + 1)

            def add(index):
                index += 1
                while index <= size:
                    tree[index] += 1
                    index += index & -index

            def prefix(index):
                count = 0
                index += 1
                while index > 0:
                    count += tree[index]
                    index -= index & -index
                return count

            groups = {}
            for source, target in group:
                groups.setdefault(left[source], []).append(right[target])
            seen = 0
            for left_index in sorted(groups):
                for right_index in groups[left_index]:
                    total += seen - prefix(right_index)
                for right_index in groups[left_index]:
                    add(right_index)
                    seen += 1
        return total

    def swap_delta(layer, index, neighbors, neighbor_positions):
        """交换相邻两行后，只统计它们与一侧相邻层之间的交叉数变化。"""
        first, second = layer[index], layer[index + 1]
        xs = [neighbor_positions[node] for node in neighbors.get(first, [])]
        ys = [neighbor_positions[node] for node in neighbors.get(second, [])]
        if not xs or not ys:
            return 0
        ys.sort()
        greater = equal = 0
        for value in xs:
            left = bisect.bisect_left(ys, value)
            greater += left
            equal += bisect.bisect_right(ys, value) - left
        total = len(xs) * len(ys)
        less = total - greater - equal
        return less - greater

    def transpose(allow_equal):
        """相邻交换下降：delta < 0 才换；allow_equal 用于走出等代价平台。"""
        changed = False
        for column, layer in enumerate(per_layer):
            index = 0
            while index < len(layer) - 1:
                if node_group(layer[index]) != node_group(layer[index + 1]):
                    index += 1
                    continue
                delta = 0
                if column + 1 < columns:
                    delta += swap_delta(layer, index, nexts, pos[column + 1])
                if column:
                    delta += swap_delta(layer, index, prevs, pos[column - 1])
                if delta < 0 or (allow_equal and delta == 0):
                    layer[index], layer[index + 1] = layer[index + 1], layer[index]
                    pos[column][layer[index]] = index
                    pos[column][layer[index + 1]] = index + 1
                    changed = True
                index += 1
        return changed

    best_order = [list(layer) for layer in per_layer]
    best = crossing_count()
    for _ in range(4):
        if not transpose(False):
            break
        current = crossing_count()
        if current < best:
            best, best_order = current, [list(layer) for layer in per_layer]
        if current == 0:
            break
    if best:
        # 等代价侧移可以走出重心排序的平台；一轮等代价交换不改变交叉数，
        # 之后的严格下降再决定是否保留更好的结果。
        if transpose(True):
            best_order = [list(layer) for layer in per_layer]
            best = crossing_count()
            while best and transpose(False):
                current = crossing_count()
                if current < best:
                    best, best_order = current, [list(layer) for layer in per_layer]
                if current == 0:
                    break
    per_layer = best_order
    positions_out = {}
    for column, layer in enumerate(per_layer):
        for row, node in enumerate(layer):
            positions_out[node] = (column, row)
    nodes = []
    for column, layer in enumerate(per_layer):
        for row, node in enumerate(layer):
            if node in tasks_by_id:
                nodes.append({"id": node, "row": row, "layer": column,
                              "progress": node_progress(tasks_by_id[node])})
    graph_edges = []
    for (source, target), chain in zip(edges, chains):
        waypoints = [{"layer": positions_out[node][0], "row": positions_out[node][1]}
                     for node in chain[1:-1]]
        graph_edges.append({"source": source, "target": target, "waypoints": waypoints})
    graph = {
        "nodes": nodes, "edges": graph_edges,
        "columns": columns, "rows": max((len(layer) for layer in per_layer), default=0),
    }
    return graph, views


def budget(plan, now=None, estimates=None):
    """余额 × 当前费率 × 剩余计划能推出的全部结论：缺数据就缺结论，绝不用 0 顶替。

    费率按实例当前的开机模式取：无卡开机按无卡费率（0.1 元/小时，不是免费），有卡按 GPU 时价。
    金额一律只按现金余额 assets 计算，代金券单独展示——否则「2 元现金 + 500 元代金券」
    会得出「还能跑 1 小时」这种吓人的错误结论。
    """
    now = now or utcnow()
    estimates = forecast(plan, now) if estimates is None else estimates
    wallet = plan["wallet"]
    assets = wallet["assets"]
    rates = {item["id"]: instance_rate(item) for item in plan["instances"]}
    result = {}
    if assets is not None:
        result["assets"] = round(assets, 2)
        if wallet["voucher"]:
            result["voucher"] = round(wallet["voucher"], 2)
        if wallet["observed_at"]:
            result["observed_at"] = wallet["observed_at"]
            # 观测多久之前是算出来的，不是展示方算的：面板与 CLI 共用 age_text，只有一份口径。
            result["observed_age"] = age_text(wallet["observed_at"], now)
        awake = [item for item in plan["instances"] if item["status"] in AWAKE]
        # 任一开机实例缺费率，烧钱速度就是残缺的；低估会让人以为还能跑很久，所以宁可不下结论。
        if awake and all(rates[item["id"]] is not None for item in awake):
            result["burn_rate"] = round(sum(rates[item["id"]] for item in awake), 4)
            result["hours_awake"] = round(assets / result["burn_rate"], 1)
    # 剩余计划按"实例最后完成时间"衡量：AutoDL 按开机时长计费，这比累加任务耗时更贴近真实花销。
    lanes, unknown = {}, False
    for task in plan["tasks"]:
        resource = task["instance"]
        if resource in (None, "local") or task["status"] in TERMINAL:
            continue
        finish = estimates[task["id"]]["finish"]
        if finish is None:
            unknown = True
            continue
        moment = date(finish)
        if resource not in lanes or moment > lanes[resource]:
            lanes[resource] = moment
    if lanes and not unknown:
        hours = {key: max(0.0, (moment - now).total_seconds() / 3600) for key, moment in lanes.items()}
        result["plan_gpu_hours"] = round(sum(hours.values()), 1)
        # 这里故意用 GPU 时价而不是 instance_rate()：待跑的任务将来是在有卡模式下跑的，
        # 按无卡费率去估会把花费压低（"余额够"的假结论），缺 GPU 时价就少给结论。
        prices = {item["id"]: item["price"] for item in plan["instances"]}
        if all(prices.get(key) is not None for key in hours):
            result["plan_cost"] = round(sum(hours[key] * prices[key] for key in hours), 2)
            if assets is not None:
                result["covers_plan"] = assets >= result["plan_cost"]
    return result


def estimate_view(plan, now=None, estimates=None):
    """结算快照 = 实时预算里"算得出的那几个结论"；缺数据就缺键，不拿 0 顶替。"""
    view = budget(plan, now, estimates)
    return {key: view[key] for key in ESTIMATE_KEYS if key in view}


def plan_fingerprint(plan):
    """结算结论只由这些字段推出，用它们的指纹判断"结算之后计划有没有变过"。

    不能拿 revision 当判据：monitor 自己每轮都要更新 last_run，用完 revision 会永远显示"有变更"。
    """
    # 开机模式与无卡费率同样进指纹：它们会改变 burn_rate/hours_awake，改了就该判成"结算后计划有变更"。
    # 老计划缺这两个键，所以用 .get 读成 None，而不是让指纹报错。
    payload = [[item.get(key) for key in ("id", "status", "mode", "price", "no_gpu_price")]
               for item in plan["instances"]]
    payload += [[task[key] for key in ("id", "status", "deps", "instance", "duration_minutes",
                                       "not_before", "started_at", "finished_at", "progress",
                                       "progress_at")] for task in plan["tasks"]]
    payload.append([plan["wallet"][key] for key in ("assets", "voucher", "observed_at")])
    blob = json.dumps(sorted(payload, key=str), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def settlement_summary(view):
    """把一份预算结论压成一句话：结算记录、show 与面板共用同一份文案，不各写一套。"""
    parts = []
    if view.get("plan_gpu_hours") is not None:
        owed = f"跑完剩余计划 {view['plan_gpu_hours']:g} GPU 小时"
        if view.get("plan_cost") is not None:
            owed += f"、约 ¥{view['plan_cost']:.2f}"
        parts.append(owed)
    if view.get("covers_plan") is not None:
        parts.append("余额够" if view["covers_plan"] else "余额不足")
    return " · ".join(parts) or "数据不足，暂无结论"


def settlement(plan, now=None, estimates=None, live_values=None):
    """show 与面板共用的结算视图：文案与"是否醒目"都在服务端定死，展示方不做判断。

    有新鲜结算（≤ 2 个周期）就用结算结论并注明结算时间；过期或从未结算则回退实时预算
    并说明原因——绝不同时给出两个互相矛盾的数字。调用方已经算过预算时把
    estimates / live_values 传进来，避免同一份计划在一轮请求里重复排程与结算。
    """
    now = now or utcnow()
    monitor = plan["monitor"]
    estimate = monitor.get("estimate")
    interval = monitor.get("interval_minutes") or DEFAULT_INTERVAL_MINUTES
    view = {"live": True, "over": False, "text": "", "values": {}, "computed_at": None, "age": None,
            "changed": False, "error": monitor.get("last_error") or "",
            "token_ready": bool(monitor.get("token_ready"))}
    if estimate:
        computed = date(estimate["computed_at"])  # age_text 收 ISO 字符串，这里要的是时刻本身
        view["computed_at"], view["age"] = estimate["computed_at"], age_text(estimate["computed_at"], now)
        if (now - computed).total_seconds() <= 2 * interval * 60:
            view["live"] = False
            view["values"] = {key: estimate[key] for key in ESTIMATE_KEYS if key in estimate}
            # 结算之后再改过计划，这份结论就不再代表当前计划——必须标出来，别让人当真。
            view["changed"] = estimate.get("plan_hash") != plan_fingerprint(plan)
            view["text"] = f"自动结算于 {view['age']} · {settlement_summary(estimate)}"
            view["over"] = estimate.get("covers_plan") is False
        else:
            view["text"] = f"自动结算已过期（{view['age']}，超过 2 个周期）· 当前为实时值"
    else:
        view["text"] = "尚未自动结算 · 开面板（serve --interval）并在面板上登记 Token 后自动结算"
    if view["live"]:
        view["values"] = ({key: live_values[key] for key in ESTIMATE_KEYS if key in live_values}
                          if live_values is not None else estimate_view(plan, now, estimates))
    if view["error"]:
        view["text"] += " · 上次结算失败"
        view["over"] = True
    if not view["token_ready"]:
        view["text"] += " · 未配置 Token"
    if view["changed"]:
        view["text"] += " · 结算后计划有变更"
    return view


def snapshot(plan, now=None):
    """面板与 show --json 看到的完整视图：账本 + 排程 + 依赖图 + 预算 + 结算。

    凡是"算出来的"都在这里算完：面板不做业务计算，只把字段摆到页面上。
    """
    now = now or utcnow()
    estimates = forecast(plan, now)
    graph, views = layout(plan, now, estimates)
    spend = budget(plan, now, estimates)
    counts = {status: 0 for status in STATUSES}
    running, queued = {}, {}
    for task in plan["tasks"]:
        counts[task["status"]] += 1
        resource = task["instance"]
        if resource in (None, "local"):
            continue
        if task["status"] == "running":
            running.setdefault(resource, []).append(task["id"])
        elif task["status"] in ("pending", "blocked"):
            queued[resource] = queued.get(resource, 0) + 1
    return {**plan, "generated_at": stamp(now), "budget": spend,
            "settlement": settlement(plan, now, estimates, spend),
            "stats": {"flows": len(plan["flows"]), "tasks": len(plan["tasks"]), "counts": counts},
            "instances": [{**item, "view": {"running": sorted(running.get(item["id"], []), key=natural_id),
                                            "queued": queued.get(item["id"], 0)}}
                          for item in plan["instances"]],
            "flows": [{**flow, "view": views[flow["id"]]}
                      for flow in plan["flows"]],
            "graph": graph,
            "tasks": [{**task, "estimate": estimates[task["id"]]}
                      for task in plan["tasks"]]}


def check_structure(plan, now=None, *, amounts=True):
    """Structural check for dependency topology and resource/duration readiness.

    issues 是需要修正的结构缺陷（ok 为假），warnings 只是观察到的现象，不影响 ok。
    """
    now = now or utcnow()
    validate(plan)
    issues, warnings = [], []
    tasks_by_id = {t["id"]: t for t in plan["tasks"]}
    grouped = {flow["id"]: [] for flow in plan["flows"]}
    for task in plan["tasks"]:
        grouped[task["flow"]].append(task)
    for flow in plan["flows"]:
        flow_tasks = grouped[flow["id"]]
        if not flow_tasks:
            issues.append(f"{flow['id']}({flow['name']}): 流程为空，无任务节点")
            continue
        for task in flow_tasks:
            if task["status"] not in TERMINAL:
                if task["instance"] is None:
                    issues.append(f"{task['id']}: 未分配实例（I01.. 或 local）")
                if task["duration_minutes"] is None:
                    target = warnings if task["status"] == "pending" else issues
                    target.append(f"{task['id']}: 未设置预计耗时（duration_minutes），完成时间待估")
            if task["started_at"] and task["deps"]:
                dep_finishes = [date(tasks_by_id[d]["finished_at"]) for d in task["deps"] if tasks_by_id[d]["finished_at"]]
                if dep_finishes:
                    gap_min = (date(task["started_at"]) - max(dep_finishes)).total_seconds() / 60
                    if gap_min > 5:
                        warnings.append(f"{task['id']}: 实际开始时间晚于前置依赖完成时间 {int(gap_min)} 分钟，存在时间空隙")
    # 以下三条都只在"数据已记录且有问题"时才出现：没有观测就不该凭空冒出提醒。
    for item in plan["instances"]:
        for _, field, label in DISK_MOUNTS:
            used, total = item["disk"][f"{field}_used_gb"], item["disk"][f"{field}_total_gb"]
            if used is None or not total or used / total < FULL_RATIO:
                continue
            warnings.append(f"{item['id']}: {label}已用 {used:g}/{total:g}G"
                            f"（{used / total * 100:.0f}%），请先清理或扩容再开新任务")
    estimates = forecast(plan, now)
    view = budget(plan, now, estimates)
    if view.get("assets") is not None:
        missing = [item["id"] for item in plan["instances"]
                   if item["status"] in AWAKE and instance_rate(item) is None]
        if missing:
            warnings.append("未登记时价（" + ", ".join(missing) +
                            "），无法把余额换算成可用时长；请打开面板（serve）在实例行上登记")
        if view.get("covers_plan") is False:
            detail = "实时预算提醒：余额不足以覆盖剩余计划"
            if amounts:
                detail = f"余额 ¥{view['assets']:.2f} 不足以覆盖剩余计划（预估 ¥{view['plan_cost']:.2f}）"
            warnings.append(detail + "，请充值或缩减计划")
    # 自动结算相关提醒：只报"正在计费却没人算账"和"算了但失败了"这两种要处置的情况。
    if plan["monitor"].get("last_error"):
        warnings.append("自动结算失败：" + plan["monitor"]["last_error"]
                        + "；面板进程会按周期重试，Token 填错或过期就在面板上重新登记")
    if any(item["status"] in AWAKE for item in plan["instances"]) and \
            settlement(plan, now, estimates, view)["live"]:
        warnings.append("有实例开机计费，但自动结算未在运行；开着面板（serve --interval）"
                        "才会定时重算余额与剩余计划的 GPU 小时")
    return {"ok": not issues, "flows": len(plan["flows"]), "tasks": len(plan["tasks"]),
            "issues": issues, "warnings": warnings}


def age_text(moment, now=None):
    """余额是易腐数据：给人看'3 小时前'，比让人自己换算 ISO 时间戳更有用。"""
    seconds = max(0, int(((now or utcnow()) - date(moment)).total_seconds()))
    if seconds < 90:
        return "刚刚"
    if seconds < 5400:
        return f"{seconds // 60} 分钟前"
    if seconds < 36 * 3600:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"



__all__ = [
    "forecast", "rounded", "node_progress", "layout", "budget", "estimate_view",
    "plan_fingerprint", "settlement_summary", "settlement", "snapshot", "check_structure",
    "age_text",
]
