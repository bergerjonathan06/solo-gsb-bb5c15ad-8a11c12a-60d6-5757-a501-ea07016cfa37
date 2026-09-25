# 胶片数字化帧号复核 API

按扫描顺序接收 2～100 帧的 OCR 帧号识别结果，基于动态规划为每帧指派一个
**严格递增**的帧号：自动舍弃误识别候选、推断未识别帧、标出缺失帧号，并给出
最优方案的代价明细。

另有胶片条方向复核接口：接收按实体顺序排列的 **2～4 条胶片条**（每条至少
1 帧、总计 2～100 帧），在“每条保持原序或整体倒序、条间顺序固定”的所有
方向组合上做同样的最优指派，并返回选定的方向与重排结果。

还有复扫规划接口：在逐帧复核结果上取**缺失帧号**与 `source=inferred` 的
**推断帧号**，合并去重为复扫目标，将其划分为若干互不重叠的连续帧号窗口，
在单窗长度上限与固定准备成本下求覆盖全部目标的**最低总成本**窗口方案。

- 语言/框架：Python 3.12 + FastAPI + Pydantic v2
- 算法：后缀动态规划（单调队列优化 + 字典序同分裁决）+ 方向组合枚举（最多 16 种）
  + 复扫窗口前缀动态规划（字典序同分裁决）
- 置信分使用 `Decimal` 精确比较（DP 内部统一放大为整数），避免浮点同分误判

## 一条命令启动

需要已安装 Docker（含 Compose 插件），在项目根目录执行：

```bash
docker compose up --build -d
```

一条命令即完成镜像构建、后台启动与健康检查注册，容器内、外端口均为
`8000`。启动后可直接使用三个接口：

- API 地址：`http://localhost:8000`
- 交互式文档（Swagger UI，三个接口均可在线调试）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 逐帧复核：`POST http://localhost:8000/api/v1/frame-reviews`
- 复扫规划：`POST http://localhost:8000/api/v1/rescan-plans`
- 胶片条方向复核：`POST http://localhost:8000/api/v1/strip-direction-reviews`

停止服务：

```bash
docker compose down
```


## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/frame-reviews` | 逐帧复核：单条扫描序列的帧号指派 |
| `POST` | `/api/v1/rescan-plans` | 复扫规划：覆盖缺失帧与推断帧的最低成本窗口方案 |
| `POST` | `/api/v1/strip-direction-reviews` | 胶片条方向复核：2～4 条胶片条的方向选择与帧号指派 |

### `POST /api/v1/frame-reviews`

#### 请求参数

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `first` | int | 固定首帧号 |
| `last` | int | 固定末帧号，必须严格大于 `first` |
| `max_missing_between` | int | 相邻两帧之间最多允许缺帧数，0～10 |
| `frames` | 数组 | 按扫描顺序排列的 2～100 帧 |
| `frames[].id` | string \| int | 扫描帧唯一标识 |
| `frames[].candidates` | 数组 | OCR 候选，可为空 |
| `candidates[].frame_number` | int | OCR 识别帧号 |
| `candidates[].confidence` | number | OCR 置信分，0～100 |

#### 指派规则与代价

- 第 1 帧必须指派为 `first`，最后 1 帧必须指派为 `last`；
- 相邻指派帧号之差 ∈ `[1, max_missing_between + 1]`；
- 命中候选代价 = `100 - 置信分`；未命中任何候选（纯推断）代价 = `100`；
- 相邻两帧之间每缺 1 帧额外加 `30`；
- 取总代价最小的方案；总代价相同时，取**帧号序列字典序最小**的方案；
- 同一帧内同帧号出现多次时，仅保留置信分最高的一条（同分保留下标小者），
  其余作为重复候选舍弃；超出 `[first, last]` 的候选也会被舍弃。
- 在上述约束下无法形成合法序列时返回 HTTP `422`，错误体含
  `error`（`invalid_range` / `no_feasible_sequence`）与中文 `message`。

#### 请求示例

```bash
curl -s -X POST http://localhost:8000/api/v1/frame-reviews \
  -H 'Content-Type: application/json' \
  -d '{
    "first": 10,
    "last": 13,
    "max_missing_between": 2,
    "frames": [
      {"id": "scan-001", "candidates": [{"frame_number": 10, "confidence": 95}]},
      {"id": "scan-002", "candidates": [{"frame_number": 12, "confidence": 90}]},
      {"id": "scan-003", "candidates": [
        {"frame_number": 13, "confidence": 80},
        {"frame_number": 99, "confidence": 70}
      ]}
    ]
  }'
```

#### 响应示例

```json
{
  "first": 10,
  "last": 13,
  "max_missing_between": 2,
  "frame_count": 3,
  "assigned_frame_numbers": [10, 12, 13],
  "total_cost": 65,
  "candidate_cost_total": 35,
  "missing_penalty_total": 30,
  "missing_frame_numbers": [11],
  "missing_frame_count": 1,
  "discarded_candidate_count": 1,
  "frames": [
    {
      "position": 1,
      "id": "scan-001",
      "assigned_frame_number": 10,
      "source": "candidate",
      "matched_candidate_index": 0,
      "assigned_cost": 5,
      "missing_after": 1,
      "missing_penalty": 30,
      "discarded_candidates": []
    },
    {
      "position": 2,
      "id": "scan-002",
      "assigned_frame_number": 12,
      "source": "candidate",
      "matched_candidate_index": 0,
      "assigned_cost": 10,
      "missing_after": 0,
      "missing_penalty": 0,
      "discarded_candidates": []
    },
    {
      "position": 3,
      "id": "scan-003",
      "assigned_frame_number": 13,
      "source": "candidate",
      "matched_candidate_index": 0,
      "assigned_cost": 20,
      "missing_after": 0,
      "missing_penalty": 0,
      "discarded_candidates": [
        {"candidate_index": 1, "frame_number": 99, "confidence": 70, "reason": "out_of_range"}
      ]
    }
  ]
}
```

舍弃候选的 `reason`：

- `not_selected`：在最优方案中未被采用（典型误识别）；
- `out_of_range`：帧号超出固定的 `[first, last]` 范围；
- `duplicate_frame_number`：同帧内与更高置信分的候选帧号重复。

#### 无合法序列示例

帧号区间无法容纳全部扫描帧（如 3 帧却只给了 2 个帧号）：

```json
{
  "error": "no_feasible_sequence",
  "message": "帧号区间 [1, 2] 只有 2 个帧号，无法为 3 帧指派严格递增的帧号"
}
```

### `POST /api/v1/rescan-plans`

在逐帧复核请求之上增加两个窗口参数。服务端先完整执行一遍逐帧复核
（与 `/api/v1/frame-reviews` 完全相同的指派、校验与错误语义），取：

- **缺失帧号**：`[first, last]` 内未指派给任何扫描帧的帧号；
- **推断帧号**：逐帧结果中 `source = "inferred"` 的指派帧号（附对应帧 ID）。

两者**合并去重**后按帧号升序作为复扫目标，再将目标划成若干
**互不重叠、按帧号升序的连续窗口**（窗口起止点均为目标帧号；向目标外
延伸只会增加成本，不会出现在最优方案中）。

#### 请求参数

逐帧复核的全部字段（`first` / `last` / `max_missing_between` / `frames`）外加：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `max_window_length` | int | 单个窗口允许的最大帧号长度（`end - start + 1`），1～50 |
| `setup_cost` | int | 每个窗口的固定准备成本，0～1000 |

#### 成本与裁决

- 单窗成本 = `setup_cost + 窗口帧号数`（窗口帧号数含区间内的非目标帧号）；
- 目标：覆盖全部复扫目标的**总成本最低**；
- 总成本相同时，**窗口数更少**者优先；
- 仍相同时，窗口**起止点序列（起点1、终点1、起点2、终点2……）字典序
  更小**者优先；
- 没有复扫目标（既无缺失帧也无推断帧）时返回空窗口数组与零成本。

参数非法（`invalid_range`）或无合法指派（`no_feasible_sequence`）时同样
返回 HTTP `422`，错误体与逐帧复核一致。

#### 请求示例

```bash
curl -s -X POST http://localhost:8000/api/v1/rescan-plans \
  -H 'Content-Type: application/json' \
  -d '{
    "first": 10,
    "last": 17,
    "max_missing_between": 3,
    "max_window_length": 4,
    "setup_cost": 5,
    "frames": [
      {"id": "scan-001", "candidates": [{"frame_number": 10, "confidence": 95}]},
      {"id": "scan-002", "candidates": [{"frame_number": 12, "confidence": 90}]},
      {"id": "scan-003", "candidates": []},
      {"id": "scan-004", "candidates": [{"frame_number": 17, "confidence": 80}]}
    ]
  }'
```

上例复核结果为：第 3 帧无候选，指派帧号 `13`（推断）；缺失帧号为
`11、14、15、16`。复扫目标为 `11、13、14、15、16`。

#### 响应示例

```json
{
  "first": 10,
  "last": 17,
  "max_missing_between": 3,
  "frame_count": 4,
  "max_window_length": 4,
  "setup_cost": 5,
  "target_frame_numbers": [11, 13, 14, 15, 16],
  "target_count": 5,
  "missing_frame_count": 4,
  "inferred_frame_count": 1,
  "window_count": 2,
  "total_cost": 15,
  "windows": [
    {
      "index": 1,
      "start_frame_number": 11,
      "end_frame_number": 11,
      "length": 1,
      "missing_frame_numbers": [11],
      "inferred_frame_ids": [],
      "window_cost": 6
    },
    {
      "index": 2,
      "start_frame_number": 13,
      "end_frame_number": 16,
      "length": 4,
      "missing_frame_numbers": [14, 15, 16],
      "inferred_frame_ids": ["scan-003"],
      "window_cost": 9
    }
  ]
}
```

字段说明：

- `target_frame_numbers`：缺失帧号与推断帧号合并去重后的全部复扫目标；
  `missing_frame_count` / `inferred_frame_count` 分别为其中两类的数量
  （两类帧号天然不相交）；
- `windows[].start_frame_number` / `end_frame_number`：窗口起止帧号（含端点）；
  `length` 为窗口帧号长度，必须不超过 `max_window_length`；
- `windows[].missing_frame_numbers`：该窗覆盖的缺失帧号；
  `windows[].inferred_frame_ids`：该窗覆盖的推断帧 ID，按帧号升序；
- `windows[].window_cost` = `setup_cost + length`；顶层
  `total_cost` 为各窗成本之和，无复扫目标时为 `0`、`windows` 为 `[]`。

### `POST /api/v1/strip-direction-reviews`

#### 请求参数

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `first` | int | 固定首帧号 |
| `last` | int | 固定末帧号，必须严格大于 `first` |
| `max_missing_between` | int | 相邻两帧之间最多允许缺帧数，0～10 |
| `strips` | 数组 | 按实体顺序排列的 2～4 条胶片条，**条间顺序固定** |
| `strips[].frames` | 数组 | 该条按扫描顺序录入的帧，至少 1 帧；总计 2～100 帧 |
| `frames[].id` | string \| int | 扫描帧唯一标识 |
| `frames[].candidates` | 数组 | OCR 候选，可为空（结构同逐帧复核） |
| `candidates[].frame_number` | int | OCR 识别帧号 |
| `candidates[].confidence` | number | OCR 置信分，0～100 |

每条胶片条要么**保持原序**（方向 `0`），要么**整条倒序**（方向 `1`，
候选随帧一起翻转）。在所有 2ⁿ（n = 条数，最多 16）种方向组合上做帧号
指派，沿用逐帧复核的全部规则：

- 第 1 个重排帧固定 `first`、最后一个固定 `last`，帧号严格递增；
- 相邻指派帧号之差 ∈ `[1, max_missing_between + 1]`；
- 命中候选代价 = `100 - 置信分`，纯推断代价 = `100`，每缺 1 帧加 `30`。

最优方案的裁决顺序（逐级字典序）：

1. **总代价最小**；
2. 总代价相同时，**倒序条数最少**；
3. 再相同时，**方向向量字典序最小**（各条按条序，正序 `0`、倒序 `1`）；
4. 仍相同时，全局**指派帧号序列字典序最小**。

所有方向组合都不存在合法指派时返回 HTTP `422`，错误码为
`no_feasible_combination`；参数非法为 `invalid_range`。

#### 请求示例

```bash
curl -s -X POST http://localhost:8000/api/v1/strip-direction-reviews \
  -H 'Content-Type: application/json' \
  -d '{
    "first": 1,
    "last": 4,
    "max_missing_between": 1,
    "strips": [
      {"frames": [
        {"id": "A1", "candidates": [{"frame_number": 1, "confidence": 99}]},
        {"id": "A2", "candidates": [{"frame_number": 2, "confidence": 90}]}
      ]},
      {"frames": [
        {"id": "B2", "candidates": [{"frame_number": 4, "confidence": 90}]},
        {"id": "B1", "candidates": [{"frame_number": 3, "confidence": 90}]}
      ]}
    ]
  }'
```

#### 响应示例

```json
{
  "first": 1,
  "last": 4,
  "max_missing_between": 1,
  "strip_count": 2,
  "frame_count": 4,
  "directions": [0, 1],
  "reversed_strip_count": 1,
  "strip_frame_ids": [["A1", "A2"], ["B1", "B2"]],
  "frame_ids": ["A1", "A2", "B1", "B2"],
  "assigned_frame_numbers": [1, 2, 3, 4],
  "total_cost": 31,
  "candidate_cost_total": 31,
  "missing_penalty_total": 0,
  "missing_frame_numbers": [],
  "missing_frame_count": 0,
  "discarded_candidate_count": 0,
  "strips": [
    {
      "index": 1,
      "direction": 0,
      "frame_count": 2,
      "frame_ids": ["A1", "A2"],
      "frames": [
        {
          "position": 1,
          "id": "A1",
          "assigned_frame_number": 1,
          "source": "candidate",
          "matched_candidate_index": 0,
          "assigned_cost": 1,
          "missing_after": 0,
          "missing_penalty": 0,
          "discarded_candidates": []
        },
        {
          "position": 2,
          "id": "A2",
          "assigned_frame_number": 2,
          "source": "candidate",
          "matched_candidate_index": 0,
          "assigned_cost": 10,
          "missing_after": 0,
          "missing_penalty": 0,
          "discarded_candidates": []
        }
      ]
    },
    {
      "index": 2,
      "direction": 1,
      "frame_count": 2,
      "frame_ids": ["B1", "B2"],
      "frames": [
        {
          "position": 3,
          "id": "B1",
          "assigned_frame_number": 3,
          "source": "candidate",
          "matched_candidate_index": 0,
          "assigned_cost": 10,
          "missing_after": 0,
          "missing_penalty": 0,
          "discarded_candidates": []
        },
        {
          "position": 4,
          "id": "B2",
          "assigned_frame_number": 4,
          "source": "candidate",
          "matched_candidate_index": 0,
          "assigned_cost": 10,
          "missing_after": 0,
          "missing_penalty": 0,
          "discarded_candidates": []
        }
      ]
    }
  ],
  "frames": [
    "……与 strips[].frames 内容相同，按全局 position 1～4 平铺，字段同逐帧复核"
  ]
}
```

字段说明：

- `directions`：各条选定方向（与输入 `strips` 同序），`0`=保持原序、
  `1`=整体倒序；`reversed_strip_count` 为其中倒序条数；
- `strip_frame_ids` / `strips[].frame_ids`：每条按选定方向**重排后的帧
  ID**；`frame_ids` 是全部帧拼接（条间固定顺序）后的结果；
- `strips[].frames[]` 为该条逐帧指派，其中 `position` 是全局位置；
  顶层 `frames` 则是全部帧按全局顺序展开的同一明细；
- 代价汇总、`missing_*`、`discarded_candidates` 字段含义与逐帧复核一致。

#### 无可行方向组合示例

```json
{
  "error": "no_feasible_combination",
  "message": "2 条胶片条的 4 种方向组合均无法形成合法的严格递增帧号序列"
}
```

## 算法说明

令 `span = last - first`，第 i 帧的指派帧号写作 `first + d_i`。
合法条件：`d_0 = 0`、`d_{n-1} = span`、
`1 ≤ d_{i+1} - d_i ≤ max_missing_between + 1`。

后缀 DP（`app/solver.py`）：

```
dp[i][d] = match(i, d)
           + min over d' ∈ [d+1, d+max_step]  (30·(d'-d-1) + dp[i+1][d'])

match(i, d) = 100 - 置信分   若该帧存在帧号 first+d 的候选
            = 100            否则（推断）
```

- 转移最小值改写为滑动窗口（`30·d' + dp[i+1][d']` 在定宽窗口内取最小），
  用单调队列维护，单层 DP 为 O(span)，单方向组合整体
  O(n · span)；方向组合最多 16 个；
- 回溯时在保持最优代价的前提下每一步选最小偏移量，后缀最优与历史无关，
  因此贪心回溯得到的就是字典序最小的最优帧号序列；
- 胶片条方向复核把每条按候选方向翻转后拼接（条间顺序不变），对每个方向
  向量跑一次上述 DP，再按“总代价 → 倒序条数 → 方向向量 → 帧号序列”
  逐级取字典序最小。

复扫窗口规划（`solve_rescan_plan`）先复用上述复核得到复扫目标
`t₁ < t₂ < … < tₘ`，窗口起止点必为目标帧号。设窗口长度上限 `L`、
准备成本 `S`，以前缀 DP 求解：

```
dp[k] = 覆盖前 k 个目标的最优方案
dp[k] = min over i 满足 t[k-1] - t[i] + 1 ≤ L  of
        dp[i] + (S + t[k-1] - t[i] + 1，新增 1 个窗口 (t[i], t[k-1]))
```

每个状态的裁决键为 `(总成本, 窗口数, (起点1,终点1,起点2,终点2,…))`，
元组按自然字典序比较，恰好实现“总成本最低 → 窗口数最少 → 起止点序列
字典序最小”的裁决。复杂度 O(m²)，而目标数最多为帧号区间长度
（≤ 999 + 100，工程规模下可忽略）。`m = 0`（无复扫目标）时直接返回
空窗口与零成本，不进入 DP。

## 本地开发（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py       # FastAPI 入口与错误处理
│   ├── schemas.py    # Pydantic 请求/响应模型
│   └── solver.py     # DP 核心（纯标准库，可独立调用）
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```
