"""帧号指派的动态规划核心。

问题描述
--------
扫描得到 n 帧（按扫描顺序排列），每帧有 0 个或多个 OCR 候选帧号及置信分。
已知整卷胶片的首帧号 ``first``、末帧号 ``last``，以及相邻两帧之间最多允许
跳过 ``max_missing_between`` 个胶片帧号。

- 候选命中代价：100 - 置信分（0～100）
- 该帧没有命中任何候选（纯推断）：100
- 相邻两帧之间每缺 1 帧：+30

要求给每帧指派一个严格递增的帧号，使总代价最小；总代价相同时，帧号序列
按字典序取最小。第一帧指派为 ``first``、最后一帧指派为 ``last``。

胶片条方向复核（:func:`solve_strip_directions`）在此基础上再枚举 2～4 条
胶片条各自的方向（原序 / 整条倒序），条间顺序固定，在所有方向组合的合法
指派中按总代价、倒序条数、方向向量、指派帧号序列依次裁决。

复扫窗口规划（:func:`solve_rescan_plan`）复用复核结果，把缺失帧号与
source=inferred 帧的指派帧号合并去重为复扫目标，再划分成互不重叠、
长度受限的连续帧号窗口，按总成本、窗口数、起止点序列依次裁决。

实现思路
--------
令 span = last - first，第 i 帧（0-based）指派到 first + d_i。
合法序列满足 d_0 = 0、d_{n-1} = span 且
1 <= d_i - d_{i-1} <= max_missing_between + 1。

定义后缀 DP：

    dp[i][d] = 第 i 帧取 first + d 时，第 i..n-1 帧的最小总代价（不含 i 之前
               的缺帧代价）

转移：

    dp[i][d] = match_cost(i, d)
             + min over d' in [d+1, d+max_step] of
                   (30 * (d' - d - 1) + dp[i+1][d'])

把转移改写成滑动窗口最小值：

    30 * (d' - d - 1) + dp[i+1][d']
      = (30 * d' + dp[i+1][d']) - 30 * (d + 1)

按 d 从大到小扫描时，窗口 [d+1, d+max_step] 整体平移，用单调队列维护
``30 * d' + dp[i+1][d']`` 的最小值，单层 DP 复杂度降为 O(span)。

回溯时在保持最小代价的前提下每一步选最小的 d，由于后缀代价与历史无关，
这恰好得到字典序最小的最优序列。

置信分可能带小数，统一用 Decimal 精确比较，避免浮点同分误判。
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Any


# ---- 代价常量 ----
INFER_COST = Decimal("100")        # 未命中任何候选时的推断代价
MISSING_FRAME_PENALTY = 30         # 每缺 1 帧的额外代价
CONFIDENCE_BASE = Decimal("100")   # 候选代价 = 100 - 置信分

# 业务边界
MIN_FRAMES = 2
MAX_FRAMES = 100
MIN_MISSING_GAP = 0
MAX_MISSING_GAP = 10

# 胶片条方向复核的边界
MIN_STRIPS = 2
MAX_STRIPS = 4

# 复扫窗口规划的边界
MIN_WINDOW_LENGTH = 1
MAX_WINDOW_LENGTH = 50
MIN_SETUP_COST = Decimal("0")
MAX_SETUP_COST = Decimal("1000")

# 方向编码：正序=0，倒序=1
DIRECTION_FORWARD = 0
DIRECTION_REVERSED = 1


class ReviewError(Exception):
    """业务校验失败的基类，携带机器可读的 code。"""

    code = "review_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidRangeError(ReviewError):
    code = "invalid_range"


class NoFeasibleSequenceError(ReviewError):
    code = "no_feasible_sequence"


class NoFeasibleCombinationError(ReviewError):
    """胶片条的所有方向组合都不存在合法指派。"""

    code = "no_feasible_combination"


def _to_decimal(value: Any, field: str, range_desc: str = "0～100") -> Decimal:
    """把 JSON 中的数字转成 Decimal；拒绝 NaN/Infinity 等非法数值。"""
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - 统一转成业务错误
        raise InvalidRangeError(
            f"{field} 必须是 {range_desc} 之间的有限数值"
        ) from exc
    if not result.is_finite():
        raise InvalidRangeError(f"{field} 必须是 {range_desc} 之间的有限数值")
    return result


def _json_number(value: Any) -> Any:
    """把 Decimal 转回 JSON 友好的 int/float。"""
    if isinstance(value, Decimal):
        integer = value.to_integral_value()
        if integer == value:
            return int(integer)
        return float(value)
    return value


# ---- 输入校验与候选解析 ----


def _validate_endpoints(payload: dict[str, Any]) -> tuple[int, int, int]:
    """校验并返回 (first, last, max_missing_between)。"""
    first = payload.get("first")
    last = payload.get("last")
    gap = payload.get("max_missing_between")

    if not isinstance(first, int) or isinstance(first, bool):
        raise InvalidRangeError("first 必须是整数帧号")
    if not isinstance(last, int) or isinstance(last, bool):
        raise InvalidRangeError("last 必须是整数帧号")
    if last <= first:
        raise InvalidRangeError("last 必须严格大于 first（帧号按扫描方向递增）")
    if (
        not isinstance(gap, int)
        or isinstance(gap, bool)
        or not (MIN_MISSING_GAP <= gap <= MAX_MISSING_GAP)
    ):
        raise InvalidRangeError(
            f"max_missing_between 必须是 {MIN_MISSING_GAP}～{MAX_MISSING_GAP} 的整数"
        )
    return first, last, gap


def _parse_frames(
    frames: Any,
    *,
    frame_label: str = "帧",
) -> list[tuple[Any, list[dict[str, Any]]]]:
    """解析帧列表并清洗候选。

    ``frame_label`` 用于在错误信息中定位（如 "第 2 条胶片条的第 1 帧"）。
    返回 [(frame_id, cleaned_candidates)]，其中 cleaned 候选保留输入下标。
    """
    parsed: list[tuple[Any, list[dict[str, Any]]]] = []
    for i, frame in enumerate(frames):
        label = f"{frame_label}第 {i + 1} 帧"
        if not isinstance(frame, dict) or "id" not in frame:
            raise InvalidRangeError(f"{label}缺少 id 字段")
        frame_id = frame["id"]
        raw_candidates = frame.get("candidates", [])
        if not isinstance(raw_candidates, list):
            raise InvalidRangeError(f"帧 {frame_id!r} 的 candidates 必须是数组")

        cleaned: list[dict[str, Any]] = []
        for j, cand in enumerate(raw_candidates):
            if not isinstance(cand, dict):
                raise InvalidRangeError(
                    f"帧 {frame_id!r} 的第 {j + 1} 个候选格式不正确"
                )
            number = cand.get("frame_number")
            if not isinstance(number, int) or isinstance(number, bool):
                raise InvalidRangeError(
                    f"帧 {frame_id!r} 的第 {j + 1} 个候选 frame_number 必须是整数"
                )
            confidence = _to_decimal(
                cand.get("confidence"),
                f"帧 {frame_id!r} 的第 {j + 1} 个候选 confidence",
            )
            if not (Decimal("0") <= confidence <= CONFIDENCE_BASE):
                raise InvalidRangeError(
                    f"帧 {frame_id!r} 的第 {j + 1} 个候选 confidence 必须在 0～100 之间"
                )
            cleaned.append(
                {
                    "index": j,
                    "frame_number": number,
                    "confidence": confidence,
                }
            )
        parsed.append((frame_id, cleaned))
    return parsed


def _best_candidates(
    parsed: list[tuple[Any, list[dict[str, Any]]]],
) -> list[dict[int, tuple[Decimal, int]]]:
    """逐帧把同帧号候选压成最高置信分一条（同分保留下标小者）。"""
    best_by_frame: list[dict[int, tuple[Decimal, int]]] = []
    for _frame_id, candidates in parsed:
        best: dict[int, tuple[Decimal, int]] = {}
        for cand in candidates:
            number = cand["frame_number"]
            confidence = cand["confidence"]
            idx = cand["index"]
            incumbent = best.get(number)
            if incumbent is None or confidence > incumbent[0]:
                best[number] = (confidence, idx)
        best_by_frame.append(best)
    return best_by_frame


# ---- DP 核心 ----


def _cost_scale(parsed: list[tuple[Any, list[dict[str, Any]]]]) -> int:
    """求所有置信分的最大小数位数 p，返回 10**p。

    代价乘以该倍率后全部是整数，DP 内可用 int 精确比较，同分裁决语义
    与 Decimal 完全一致；输出时再除回去。
    """
    places = 0
    for _frame_id, candidates in parsed:
        for cand in candidates:
            exponent = cand["confidence"].as_tuple().exponent
            if isinstance(exponent, int) and exponent < 0:
                places = max(places, -exponent)
    return 10**places


def _solve_assignment(
    first: int,
    last: int,
    gap: int,
    parsed: list[tuple[Any, list[dict[str, Any]]]],
) -> dict[str, Any]:
    """对一条已按扫描顺序排好的帧序列执行帧号指派。

    返回内部解法（代价为乘以 cost_scale 的整数），供两个接口各自组装
    响应；无合法序列时抛 NoFeasibleSequenceError。
    """
    n = len(parsed)
    span = last - first
    max_step = gap + 1  # 相邻指派帧号之差上限（差 1 表示不缺帧）

    best_by_frame = _best_candidates(parsed)

    # ---- 可行性前置检查 ----
    if span < n - 1:
        raise NoFeasibleSequenceError(
            f"帧号区间 [{first}, {last}] 只有 {span + 1} 个帧号，"
            f"无法为 {n} 帧指派严格递增的帧号"
        )
    if span > (n - 1) * max_step:
        raise NoFeasibleSequenceError(
            f"帧号跨度为 {span}，但 {n} 帧在相邻最多缺 {gap} 帧的限制下"
            f"最多只能跨越 {(n - 1) * max_step}，无法覆盖到末帧 {last}"
        )

    # ---- 代价统一放大成整数：推断 100、缺帧 30、命中 100-置信分 ----
    scale = _cost_scale(parsed)
    infer_cost_scaled = int(INFER_COST * scale)
    penalty_scaled = MISSING_FRAME_PENALTY * scale
    hit_costs: list[dict[int, int]] = []
    for best in best_by_frame:
        hit_costs.append(
            {
                number: int((CONFIDENCE_BASE - confidence) * scale)
                for number, (confidence, _idx) in best.items()
            }
        )

    def match_cost(i: int, d: int) -> int:
        return hit_costs[i].get(first + d, infer_cost_scaled)

    # ---- 后缀 DP：layers[i] = dp[i]，dict[offset -> 后缀最小代价] ----
    layers: list[dict[int, int]] = [dict() for _ in range(n)]

    # 最后一帧必须指派 last（offset = span）
    layers[n - 1][span] = match_cost(n - 1, span)

    for i in range(n - 2, -1, -1):
        layer: dict[int, int] = {}
        # 单调队列元素 (offset, key)，按 d 从大到小扫描：
        # - 新 offset（较小）从队尾加入，超窗的大 offset 从队首淘汰；
        # - key = 30*offset + dp[i+1][offset]，从队首到队尾严格递增，
        #   队首即窗口最小值。
        # 等价于以 x = span - offset 为坐标的经典递增滑动窗口。
        queue: deque[tuple[int, int]] = deque()
        next_layer = layers[i + 1]
        for d in range(span - 1, -1, -1):
            added = d + 1
            suffix = next_layer.get(added)
            if suffix is not None:
                key = penalty_scaled * added + suffix
                while queue and queue[-1][1] >= key:
                    queue.pop()
                queue.append((added, key))
            # 窗口上界为 d + max_step，丢掉 offset 过大的队首
            if queue and queue[0][0] > d + max_step:
                queue.popleft()
            if queue:
                # min key - 30*(d+1) 即为最优后继带来的代价
                best_transition = queue[0][1] - penalty_scaled * (d + 1)
                layer[d] = match_cost(i, d) + best_transition
        layers[i] = layer

    # 第一帧必须指派 first（offset = 0）
    if 0 not in layers[0]:
        # 理论上已被前置可行性检查覆盖，保留防御性报错。
        raise NoFeasibleSequenceError("无法形成从首帧到末帧的合法帧号序列")

    total_cost = layers[0][0]

    # ---- 回溯：在最优代价前提下每一步取最小 offset ----
    # nd 从小到大遍历且只在严格更小时替换，同分自然保留最小值；
    # 后缀最优与历史无关，因此贪心得到全局字典序最小的最优序列。
    offsets: list[int] = [0]
    for i in range(n - 1):
        d = offsets[-1]
        chosen: int | None = None
        chosen_value: int | None = None
        upper = min(span, d + max_step)
        next_layer = layers[i + 1]
        for nd in range(d + 1, upper + 1):
            suffix = next_layer.get(nd)
            if suffix is None:
                continue
            value = penalty_scaled * (nd - d - 1) + suffix
            if chosen_value is None or value < chosen_value:
                chosen_value = value
                chosen = nd
        if chosen is None:  # 防御性检查
            raise NoFeasibleSequenceError(
                f"回溯到第 {i + 1} 帧时找不到合法后继帧号"
            )
        offsets.append(chosen)

    return {
        "first": first,
        "last": last,
        "gap": gap,
        "span": span,
        "n": n,
        "parsed": parsed,
        "best_by_frame": best_by_frame,
        "offsets": offsets,
        "total_cost": total_cost,
        "cost_scale": scale,
    }


# ---- 逐帧结果与代价明细 ----


def _build_frame_results(
    solution: dict[str, Any],
    *,
    position_offset: int = 0,
) -> tuple[list[dict[str, Any]], Decimal, int, list[int], list[int]]:
    """把内部解法组装成逐帧 JSON 结构。

    position_offset 用于胶片条场景让 position 取全局位置。
    返回 (frames, 命中/推断代价合计(Decimal), 舍弃候选总数,
    指派帧号序列, 各帧后缺帧数)。
    """
    first = solution["first"]
    n = solution["n"]
    offsets = solution["offsets"]
    parsed = solution["parsed"]
    best_by_frame = solution["best_by_frame"]

    result_frames: list[dict[str, Any]] = []
    sum_match_cost = Decimal(0)
    total_discarded = 0

    missing_after_list: list[int] = []
    for i in range(n):
        if i + 1 < n:
            missing_after = offsets[i + 1] - offsets[i] - 1
        else:
            missing_after = 0
        missing_after_list.append(missing_after)

    for i, (frame_id, candidates) in enumerate(parsed):
        d = offsets[i]
        number = first + d
        hit = best_by_frame[i].get(number)
        matched_index = hit[1] if hit is not None else None

        if hit is not None:
            # 直接用置信分计算精确代价，避免整数缩放反除带来的精度风险
            frame_cost = CONFIDENCE_BASE - hit[0]
            source = "candidate"
        else:
            frame_cost = INFER_COST
            source = "inferred"
        sum_match_cost += frame_cost

        missing_after = missing_after_list[i]
        gap_penalty = MISSING_FRAME_PENALTY * missing_after

        discarded: list[dict[str, Any]] = []
        for cand in candidates:
            idx = cand["index"]
            cand_number = cand["frame_number"]
            if idx == matched_index:
                continue  # 被采用的候选
            # 说明舍弃原因
            winner = best_by_frame[i].get(cand_number)
            if winner is not None and winner[1] != idx:
                reason = "duplicate_frame_number"  # 同帧号仅保留置信分最高者
            elif cand_number < first or cand_number > solution["last"]:
                reason = "out_of_range"            # 超出固定首末帧号范围
            else:
                reason = "not_selected"            # 误识别 / 未被最优方案采用
            discarded.append(
                {
                    "candidate_index": idx,
                    "frame_number": cand_number,
                    "confidence": _json_number(cand["confidence"]),
                    "reason": reason,
                }
            )

        discarded.sort(key=lambda item: item["candidate_index"])
        total_discarded += len(discarded)

        result_frames.append(
            {
                "position": position_offset + i + 1,
                "id": frame_id,
                "assigned_frame_number": number,
                "source": source,
                "matched_candidate_index": matched_index,
                "assigned_cost": _json_number(frame_cost),
                "missing_after": missing_after,
                "missing_penalty": gap_penalty,
                "discarded_candidates": discarded,
            }
        )

    assigned_numbers = [first + d for d in offsets]
    return (
        result_frames,
        sum_match_cost,
        total_discarded,
        assigned_numbers,
        missing_after_list,
    )


def _missing_frame_numbers(
    first: int, last: int, assigned_set: set[int]
) -> list[int]:
    """[first, last] 内未指派给任何扫描帧的帧号（有序）。"""
    return [num for num in range(first, last + 1) if num not in assigned_set]


# ---- 对外接口 ----


def solve_review(payload: dict[str, Any]) -> dict[str, Any]:
    """执行帧号复核。

    入参字段：
        first / last: int，固定首末帧号（last > first）
        max_missing_between: int，0～10
        frames: [{id, candidates: [{frame_number: int, confidence: 0~100}]}]

    返回逐帧指派、每帧舍弃的候选、缺失帧号与各项代价汇总。
    参数不合法时抛 InvalidRangeError，无合法序列时抛 NoFeasibleSequenceError。
    """
    frames = payload.get("frames")
    if not isinstance(frames, list) or not (MIN_FRAMES <= len(frames) <= MAX_FRAMES):
        raise InvalidRangeError(
            f"frames 数量必须在 {MIN_FRAMES}～{MAX_FRAMES} 之间"
        )

    first, last, gap = _validate_endpoints(payload)
    parsed = _parse_frames(frames)
    solution = _solve_assignment(first, last, gap, parsed)

    n = solution["n"]

    (
        result_frames,
        sum_match_cost,
        total_discarded,
        assigned_numbers,
        _missing_after_list,
    ) = _build_frame_results(solution)

    sum_missing_penalty = Decimal(
        MISSING_FRAME_PENALTY * (solution["span"] - (n - 1))
    )
    total_cost = sum_match_cost + sum_missing_penalty

    missing_frame_numbers = _missing_frame_numbers(
        first, last, set(assigned_numbers)
    )

    computed_total = sum_match_cost + sum_missing_penalty
    assert computed_total == total_cost, "代价明细与 DP 总代价不一致"
    assert total_cost == Decimal(solution["total_cost"]) / solution["cost_scale"], (
        "代价明细与整数 DP 总代价不一致"
    )
    assert len(missing_frame_numbers) == solution["span"] - (n - 1), (
        "缺失帧计数不一致"
    )

    return {
        "first": first,
        "last": last,
        "max_missing_between": gap,
        "frame_count": n,
        "assigned_frame_numbers": assigned_numbers,
        "total_cost": _json_number(total_cost),
        "candidate_cost_total": _json_number(sum_match_cost),
        "missing_penalty_total": _json_number(sum_missing_penalty),
        "missing_frame_numbers": missing_frame_numbers,
        "missing_frame_count": len(missing_frame_numbers),
        "discarded_candidate_count": total_discarded,
        "frames": result_frames,
    }


def _parse_strips(
    strips: Any,
) -> list[list[tuple[Any, list[dict[str, Any]]]]]:
    """校验并解析胶片条输入，返回每条解析后的帧序列。"""
    if not isinstance(strips, list) or not (MIN_STRIPS <= len(strips) <= MAX_STRIPS):
        raise InvalidRangeError(
            f"strips 数量必须在 {MIN_STRIPS}～{MAX_STRIPS} 之间"
        )
    parsed_strips: list[list[tuple[Any, list[dict[str, Any]]]]] = []
    total_frames = 0
    for s, strip in enumerate(strips):
        if not isinstance(strip, dict):
            raise InvalidRangeError(f"第 {s + 1} 条胶片条格式不正确")
        frames = strip.get("frames")
        if not isinstance(frames, list) or len(frames) < 1:
            raise InvalidRangeError(
                f"第 {s + 1} 条胶片条必须包含至少 1 帧"
            )
        parsed = _parse_frames(
            frames, frame_label=f"第 {s + 1} 条胶片条的"
        )
        parsed_strips.append(parsed)
        total_frames += len(parsed)
    if not (MIN_FRAMES <= total_frames <= MAX_FRAMES):
        raise InvalidRangeError(
            f"所有胶片条的帧总数必须在 {MIN_FRAMES}～{MAX_FRAMES} 之间"
            f"（当前 {total_frames} 帧）"
        )
    return parsed_strips


def solve_strip_directions(payload: dict[str, Any]) -> dict[str, Any]:
    """执行胶片条方向复核。

    入参字段：
        first / last / max_missing_between: 同 solve_review
        strips: [{frames: [{id, candidates}]}]，2～4 条，
                每条至少 1 帧，总计 2～100 帧。条间顺序固定。

    枚举每条胶片条正序 / 倒序（最多 16 种方向组合），条内倒序为整条
    翻转（候选仍随帧走）。在所有存在合法指派的组合中依次按
    总代价最小、倒序条数最少、方向向量字典序最小、指派帧号序列字典序
    最小裁决。所有组合均不可行时抛 NoFeasibleCombinationError（422）。
    """
    first, last, gap = _validate_endpoints(payload)
    parsed_strips = _parse_strips(payload.get("strips"))
    strip_sizes = [len(strip) for strip in parsed_strips]
    strip_count = len(parsed_strips)

    best_key: tuple[Any, ...] | None = None
    best: dict[str, Any] | None = None

    # 方向向量从全 0 到全 1 按二进制递增枚举，与最终字典序裁决方向一致。
    for mask in range(1 << strip_count):
        directions = tuple(
            DIRECTION_REVERSED if (mask >> (strip_count - 1 - s)) & 1
            else DIRECTION_FORWARD
            for s in range(strip_count)
        )
        ordered: list[tuple[Any, list[dict[str, Any]]]] = []
        for s, strip in enumerate(parsed_strips):
            if directions[s] == DIRECTION_REVERSED:
                ordered.extend(reversed(strip))
            else:
                ordered.extend(strip)

        try:
            solution = _solve_assignment(first, last, gap, ordered)
        except NoFeasibleSequenceError:
            continue

        # 裁决键：总代价、倒序条数、方向向量、指派帧号序列（逐级字典序）
        ranking_key = (
            solution["total_cost"],
            directions.count(DIRECTION_REVERSED),
            directions,
            tuple(first + d for d in solution["offsets"]),
        )
        if best_key is None or ranking_key < best_key:
            best_key = ranking_key
            best = {
                "directions": directions,
                "solution": solution,
            }

    if best is None:
        raise NoFeasibleCombinationError(
            f"{strip_count} 条胶片条的 {1 << strip_count} 种方向组合"
            "均无法形成合法的严格递增帧号序列"
        )

    directions = best["directions"]
    solution = best["solution"]
    n = solution["n"]

    # ---- 组装全局逐帧结果 ----
    (
        all_frames,
        sum_match_cost,
        total_discarded,
        assigned_numbers,
        _missing_after_list,
    ) = _build_frame_results(solution)

    sum_missing_penalty = Decimal(
        MISSING_FRAME_PENALTY * (solution["span"] - (n - 1))
    )
    total_cost = sum_match_cost + sum_missing_penalty
    missing_frame_numbers = _missing_frame_numbers(
        first, last, set(assigned_numbers)
    )
    assert total_cost == Decimal(solution["total_cost"]) / solution["cost_scale"], (
        "代价明细与 DP 总代价不一致"
    )

    global_frame_ids = [frame["id"] for frame in all_frames]

    # ---- 按条切片：方向、重排后的帧 ID、逐帧指派 ----
    result_strips: list[dict[str, Any]] = []
    strip_frame_ids: list[list[Any]] = []
    cursor = 0
    for s, size in enumerate(strip_sizes):
        strip_frames = all_frames[cursor : cursor + size]
        ids = [frame["id"] for frame in strip_frames]
        strip_frame_ids.append(ids)
        result_strips.append(
            {
                "index": s + 1,
                "direction": directions[s],
                "frame_count": size,
                "frame_ids": ids,
                "frames": strip_frames,
            }
        )
        cursor += size

    return {
        "first": first,
        "last": last,
        "max_missing_between": gap,
        "strip_count": strip_count,
        "frame_count": n,
        "directions": list(directions),
        "reversed_strip_count": directions.count(DIRECTION_REVERSED),
        "strip_frame_ids": strip_frame_ids,
        "frame_ids": global_frame_ids,
        "assigned_frame_numbers": assigned_numbers,
        "total_cost": _json_number(total_cost),
        "candidate_cost_total": _json_number(sum_match_cost),
        "missing_penalty_total": _json_number(sum_missing_penalty),
        "missing_frame_numbers": missing_frame_numbers,
        "missing_frame_count": len(missing_frame_numbers),
        "discarded_candidate_count": total_discarded,
        "strips": result_strips,
        "frames": all_frames,
    }


# ---- 复扫窗口规划 ----


def _plan_windows(
    targets: list[int],
    max_window_length: int,
    setup_cost: Decimal,
) -> tuple[Decimal, list[tuple[int, int]]]:
    """把升序去重的目标帧号划分为互不重叠的连续窗口。

    每窗长度（end - start + 1）不超过 ``max_window_length``，成本为
    ``setup_cost + 窗口帧号数``。返回 (最低总成本, [(start, end), ...])；
    同成本时依次取窗口数更少、起止点序列字典序更小的方案。

    最优解的窗口起止必然都是目标帧号（把窗口收紧到窗内首个/末个目标
    只会更便宜），因此问题等价于把有序目标划分成若干连续段，且段对应
    的帧号跨度受限。dp[j] 记录覆盖前 j 个目标的最优键
    (成本, 窗口数, 起止序列)，三元组按裁决顺序直接比较；向前缀最优键
    追加同一窗口时该序保持单调，故子问题最优可转移。
    """
    m = len(targets)
    if m == 0:
        return Decimal(0), []

    # dp[j] = 覆盖 targets[:j] 的最优键 (成本, 窗口数, 窗口起止序列)
    dp: list[tuple[Decimal, int, tuple[tuple[int, int], ...]] | None] = [
        None
    ] * (m + 1)
    dp[0] = (Decimal(0), 0, ())
    for j in range(1, m + 1):
        end = targets[j - 1]
        best: tuple[Decimal, int, tuple[tuple[int, int], ...]] | None = None
        # 最后一窗 [targets[i], end] 覆盖第 i..j-1 个目标（0-based）
        for i in range(j - 1, -1, -1):
            start = targets[i]
            length = end - start + 1
            if length > max_window_length:
                break
            prev = dp[i]
            assert prev is not None, "单目标窗口长度为 1，任意前缀必可达"
            key = (
                prev[0] + setup_cost + length,
                prev[1] + 1,
                prev[2] + ((start, end),),
            )
            if best is None or key < best:
                best = key
        dp[j] = best

    final = dp[m]
    assert final is not None
    return final[0], list(final[2])


def solve_rescan_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """基于逐帧复核结果规划复扫窗口。

    入参字段：solve_review 的全部字段，外加
        max_window_length: int，1～50，单个窗口最多覆盖的连续帧号数
        setup_cost: 0～1000 的数值，每开启一个窗口的固定成本

    复用 solve_review 取得缺失帧号与 source=inferred 帧的指派帧号，
    合并去重为复扫目标；把目标划分为互不重叠、长度不超过
    max_window_length 的连续帧号窗口，每窗成本 = setup_cost + 窗口
    帧号数，求覆盖全部目标的最低总成本；同成本依次取窗口数更少、
    起止点序列字典序更小的方案。没有目标时返回空窗口与零成本。
    错误语义与 solve_review 相同。
    """
    max_window_length = payload.get("max_window_length")
    if (
        not isinstance(max_window_length, int)
        or isinstance(max_window_length, bool)
        or not (MIN_WINDOW_LENGTH <= max_window_length <= MAX_WINDOW_LENGTH)
    ):
        raise InvalidRangeError(
            f"max_window_length 必须是 "
            f"{MIN_WINDOW_LENGTH}～{MAX_WINDOW_LENGTH} 的整数"
        )
    setup_cost = _to_decimal(payload.get("setup_cost"), "setup_cost", "0～1000")
    if not (MIN_SETUP_COST <= setup_cost <= MAX_SETUP_COST):
        raise InvalidRangeError("setup_cost 必须在 0～1000 之间")

    review = solve_review(payload)

    missing_numbers: list[int] = review["missing_frame_numbers"]
    inferred_frames = [
        (frame["assigned_frame_number"], frame["id"])
        for frame in review["frames"]
        if frame["source"] == "inferred"
    ]

    # 缺失帧号与推断帧号天然互斥，仍统一排序去重
    targets = sorted(set(missing_numbers) | {num for num, _ in inferred_frames})

    planned_cost, windows = _plan_windows(targets, max_window_length, setup_cost)

    inferred_id_by_number = {num: frame_id for num, frame_id in inferred_frames}

    result_windows: list[dict[str, Any]] = []
    total_cost = Decimal(0)
    covered: list[int] = []
    for start, end in windows:
        length = end - start + 1
        window_cost = setup_cost + length
        total_cost += window_cost
        window_missing = [num for num in missing_numbers if start <= num <= end]
        window_inferred = [
            num for num in range(start, end + 1) if num in inferred_id_by_number
        ]
        covered.extend(window_missing)
        covered.extend(window_inferred)
        result_windows.append(
            {
                "start": start,
                "end": end,
                "length": length,
                "cost": _json_number(window_cost),
                "missing_frame_numbers": window_missing,
                "inferred_frame_ids": [
                    inferred_id_by_number[num] for num in window_inferred
                ],
            }
        )

    assert total_cost == planned_cost, "窗口成本合计与 DP 最优成本不一致"
    assert sorted(covered) == targets, "窗口未完整覆盖全部复扫目标"

    return {
        "max_window_length": max_window_length,
        "setup_cost": _json_number(setup_cost),
        "target_frame_numbers": targets,
        "target_count": len(targets),
        "window_count": len(result_windows),
        "total_cost": _json_number(total_cost),
        "windows": result_windows,
    }
