# 可解释规则裁决 API（Explainable Rule Adjudication API）

基于 **Python + FastAPI + 内存存储** 的策略裁决服务。客户端提交由
`all`（与）、`any`（或）、`not`（非）以及比较运算组成的策略，每条规则带
**优先级**与**结论**；服务针对给定事实做裁决，并给出逐节点推导轨迹。

## 语义

- **三值逻辑（Kleene 强三值逻辑）**：每个表达式求值为 `true` / `false` / `unknown`。
  事实中缺失或为 `null` 的字段使对应比较表达式为 `unknown`；`all` / `any` 按
  三值逻辑传播（例如 `false AND unknown = false`、`true OR unknown = true`）。
- **优先级裁决**：规则按优先级从高到低分层层内裁决：
  - 层内**全部已知且至少一条为真**：真规则结论唯一 → `decided`（返回唯一结论）；
    真规则结论不一致 → `conflict`（冲突）。
  - 层内**全部为假**：跳过该层，继续下探更低优先级。
  - 层内**存在 unknown**：停止下探，返回 `unknown`，并给出
    **使该层可判定（层内每条规则都变已知）的所有包含关系最小的缺失字段集合**
    （`minimal_missing_field_sets`）以及逐节点推导轨迹。
  - 所有层均为假：返回 `no_match`。
- **包含关系最小**：集合族中删除冗余超集（如已存在 `{a}` 则删除 `{a,b}`）。
  同字段多个比较的不可能组合（如 `x>10 AND x<5`）会被可行性检查剔除。
- **可解释性**：每次裁决返回每个节点的 `true/false/unknown` 状态树、缺失字段
  对应的原子表（atom id / field / op / value）、每层裁决状态与跳过的优先级。

> 缺失字段集合是“存在一种赋值使该层可判定”的最小字段集。实现以比较原子为
> 布尔变量做最小 DNF 推导，再投影到字段并做逐字段具体可行性校验，因此能正确
> 处理同字段比较之间的关联（例如 `age>=18` 与 `age<18` 互补，只需提供 `age`）。

## 一条命令启动

```bash
docker compose up --build
```

依赖在**容器启动时**由 `entrypoint.sh` 执行 `pip install -r requirements.txt`
安装，随后启动 uvicorn。停止：`Ctrl+C` 或 `docker compose down`。

不使用容器时（需 Python 3.11+）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT`（compose 宿主机映射端口） | `8000` | 宿主机暴露端口，如 `PORT=9000 docker compose up` |
| `HOST`（容器内） | `0.0.0.0` | uvicorn 绑定地址 |
| `PORT`（容器内） | `8000` | 容器内监听端口 |
| `LOG_LEVEL` | `info` | uvicorn 日志级别 |
| `MAX_BATCH_PLAN_COMBINATIONS` | `100000` | 批量数据方案组合上限（折叠过程中生成的并集组合数），超过返回 `422` |

无数据库等其他依赖；策略保存在进程内存中，重启即清空。

## API 地址与端口

- 默认地址：**http://localhost:8000**
- 健康检查：
  - `GET http://localhost:8000/health` — 存活检查，返回 `{"status":"ok"}`
  - `GET http://localhost:8000/health/ready` — 就绪检查（含内存策略数量）
- 交互式文档：**http://localhost:8000/docs** （Swagger UI）
- 接口前缀：`/api/v1`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/policies` | 创建策略（结构校验，返回生成的 id） |
| PUT | `/api/v1/policies/{id}` | 整体替换策略 |
| GET | `/api/v1/policies` | 列出策略摘要 |
| GET | `/api/v1/policies/{id}` | 获取策略全文 |
| DELETE | `/api/v1/policies/{id}` | 删除策略 |
| POST | `/api/v1/policies/{id}/evaluate` | 提交事实进行裁决 |
| POST | `/api/v1/policies/{id}/evaluate-sources` | 多来源带优先级事实合并后裁决（逐字段只比较最高优先级来源） |
| POST | `/api/v1/policies/{id}/counterfactuals` | 反事实搜索：让裁决唯一变为目标结论的最小修改集 |
| POST | `/api/v1/policies/{id}/batch-data-plans` | 批量数据方案：让全部 unknown 案例可判定的最小全局字段集合 |
| POST | `/api/v1/policies/{id}/replacement-preview` | 替换影响预览：候选规则对案例集的业务影响（不写入存储） |
| POST | `/api/v1/adjudication-flows/evaluate` | 裁决流：内联节点图按裁决结果在已存策略间路由（不写入存储） |

## 策略结构

```json
{
  "rules": [
    {
      "id": "adult-allow",                 // 可选，缺省自动生成 rule-1...
      "priority": 100,                     // 整数，越大越优先
      "conclusion": "allow",               // 非空字符串
      "expr": {
        "all": [
          { "field": "age", "op": "ge", "value": 18 },
          { "any": [
              { "field": "country", "op": "eq", "value": "NL" },
              { "not": { "field": "blocked", "op": "eq", "value": true } }
          ]}
        ]
      }
    }
  ]
}
```

- 比较叶节点：`{"field": <字符串>, "op": <运算符>, "value": <JSON字面量>}`
- 比较运算符：`eq`、`ne`、`gt`、`ge`、`lt`、`le`
- 逻辑节点：`{"all": [节点, ...]}`、`{"any": [节点, ...]}`、`{"not": 节点}`
- 类型严格：`true != 1`，数字与字符串间排序比较结果为 `false`（不会变 unknown）
- 结构非法时返回 `422`，响应体含 `errors` 列表，例如：

```json
{ "detail": "Policy structure validation failed",
  "errors": ["rules[0].expr.op: must be one of ['eq', 'ge', 'gt', 'le', 'lt', 'ne']"] }
```

## 裁决示例

```bash
PID=$(curl -s -X POST localhost:8000/api/v1/policies \
  -H 'Content-Type: application/json' \
  -d '{"rules":[
    {"id":"approve","priority":10,"conclusion":"approve",
     "expr":{"all":[{"field":"income","op":"ge","value":50000},
                    {"field":"debt_ratio","op":"lt","value":0.4}]}},
    {"id":"reject","priority":10,"conclusion":"reject",
     "expr":{"field":"debt_ratio","op":"ge","value":0.6}}]}' \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

# 事实不足 → unknown + 最小缺失字段集合 + 轨迹
curl -s -X POST localhost:8000/api/v1/policies/$PID/evaluate \
  -H 'Content-Type: application/json' -d '{"facts":{}}'
```

unknown 响应（节选）：

```json
{
  "status": "unknown",
  "decided_priority": 10,
  "conclusion": null,
  "reason": "undecidable_level",
  "missing_fields": ["debt_ratio"],
  "minimal_missing_field_sets": [["debt_ratio"]],
  "pending_conclusions": ["approve", "reject"],
  "skipped_priorities": [],
  "level_trace": {
    "priority": 10,
    "state": "unknown",
    "rules": [ { "rule_id": "approve", "status": "unknown",
                 "expr": { "op": "all", "status": "unknown", "children": [ ... ] } },
               { "rule_id": "reject", "status": "unknown", "expr": { ... } } ],
    "atoms": [ { "id": 0, "field": "income", "op": "ge", "value": 50000 },
               { "id": 1, "field": "debt_ratio", "op": "lt", "value": 0.4 },
               { "id": 2, "field": "debt_ratio", "op": "ge", "value": 0.6 } ],
    "true_rule_ids": [],
    "minimal_missing_field_sets": [["debt_ratio"]]
  },
  "levels_trace": [ ... ]
}
```

事实充分时：

```bash
curl -s -X POST localhost:8000/api/v1/policies/$PID/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"facts":{"income":80000,"debt_ratio":0.2}}'
# {"status":"decided","conclusion":"approve","decided_priority":10,
#  "true_rule_ids":["approve"], ... ,"level_trace":{"state":"fired", ...}}
```

同优先级真规则结论相反：

```json
{ "status": "conflict", "reason": "opposite_conclusions",
  "conflicting_conclusions": ["no", "yes"], "decided_priority": 1 }
```

## 多来源事实合并裁决示例（evaluate-sources）

一次请求携带**多组具名事实来源**，每组带来源名、**整数优先级**（越大越优先）
与一组 `facts`。服务**逐字段**做合并，再把合并后的事实交给与
`/evaluate` 完全相同的裁决引擎：

- 对每个字段，只收集**提供该字段的最高优先级**来源（低优先级一律丢弃，
  记为 `lower_priority`）：
  - 最高优先级各来源的值**严格相同且非 null** → 字段 `resolved`，采用该值，
    所有一致来源列入 `adopted_sources`（严格相等沿用引擎的 JSON 类型规则，
    如 `true != 1`）；
  - 最高优先级的值**不一致** → 字段 `unresolved`（`reason` 为
    `conflicting_values`），`conflicting_values` 给出去重后的冲突值；
  - 最高优先级出现 **`null`** → 字段 `unresolved`（仅 null 时 `reason` 为
    `null_value`，null 与其他值并存时为 `conflicting_values`）。
- **低优先级来源永不回填**：最高优先级冲突或含 null 时，该字段不进入
  `merged_facts`（裁决引擎视其为缺失 → 通常导致 `unknown`），低优先级值
  仅在 `discarded_sources` 中留痕。
- 结果与来源在请求中的**排列顺序无关**：来源按 `(-priority, name)` 处理，
  字段、来源名、冲突值均做确定性排序输出。

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sources` | 非空数组 | 元素为 `{"name": <非空字符串>, "priority": <整数>, "facts": <对象>}`；`facts` 缺省视为 `{}`，可显式含 null |

非法请求（来源重名、`sources` 为空/缺失、`priority` 非整数或为布尔、
`name` 非空字符串校验失败、`facts` 不是字符串键对象、元素不是对象、含未知
键等）返回 `422`，响应体含 `errors` 列表；策略不存在返回 `404`。

```bash
curl -s -X POST localhost:8000/api/v1/policies/$PID/evaluate-sources \
  -H 'Content-Type: application/json' \
  -d '{
    "sources": [
      {"name": "bureau", "priority": 100,
       "facts": {"income": 80000, "debt_ratio": 0.2}},
      {"name": "bank",   "priority": 100, "facts": {"income": 40000}},
      {"name": "form",   "priority": 1,
       "facts": {"income": 80000, "debt_ratio": 0.2, "vip": true}}
    ]
  }'
```

上例中 `income` 在最高优先级 100 上冲突（80000 vs 40000），`form` 的
80000 不得回填，字段标记为 `unresolved`；`debt_ratio` 与 `vip` 无更高优先级
竞争，正常采用。响应（节选）：

```json
{
  "policy_id": "ab12...",
  "source_count": 3,
  "merged_facts": {"debt_ratio": 0.2, "vip": true},
  "unresolved_fields": ["income"],
  "fields": {
    "income": {
      "status": "unresolved",
      "reason": "conflicting_values",
      "adopted_sources": [],
      "discarded_sources": [
        {"source": "bank",   "priority": 100, "value": 40000, "reason": "conflicting_values"},
        {"source": "bureau", "priority": 100, "value": 80000, "reason": "conflicting_values"},
        {"source": "form",   "priority": 1,   "value": 80000, "reason": "lower_priority"}
      ],
      "conflicting_values": [40000, 80000]
    },
    "debt_ratio": {
      "status": "resolved", "value": 0.2,
      "adopted_sources": ["bureau"],
      "discarded_sources": [
        {"source": "form", "priority": 1, "value": 0.2, "reason": "lower_priority"}
      ]
    },
    "vip": {"status": "resolved", "value": true,
            "adopted_sources": ["form"], "discarded_sources": []}
  },
  "verdict": { "status": "unknown", "conclusion": null,
               "missing_fields": ["income"], "level_trace": { "...": "..." } }
}
```

- `fields` 逐字段给出**采用来源**（`adopted_sources`）与**丢弃来源**
  （`discarded_sources`，含来源名、优先级、该来源提供的值与丢弃原因）；
  `unresolved` 字段额外给出 `conflicting_values`（null 参与时包含 `null`）。
- `verdict` 是用 `merged_facts` 调用现有裁决引擎得到的**完整裁决结果**
  （结构与 `/evaluate` 一致，含 `levels_trace` / `level_trace` 等轨迹）；
  unresolved 字段等价于事实缺失，因此三值逻辑、最小缺失字段集合等语义全部
  原样适用。

## 反事实修改示例（counterfactuals）

在给定事实基础上，按字段提供候选修改值，搜索能让裁决**唯一变为目标结论**
的所有**包含关系最小**修改集（同一字段一次只能选一个候选值）。

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `facts` | object | 原始事实（缺省视为 `{}`，缺失/null 字段仍为 unknown） |
| `target_conclusion` | 非空字符串 | 希望最终唯一得到的结论 |
| `candidates` | 非空 object | 字段名 → 该字段可选的候选值**非空数组**（每个组合每字段至多取一个值） |
| `max_changes` | 整数 1–100 | 最多允许修改的字段数 |

非法请求（`candidates` 为空、某字段候选值为空、`max_changes` 非法等）返回
`422`，响应体含 `errors` 列表。

```bash
curl -s -X POST localhost:8000/api/v1/policies/$PID/counterfactuals \
  -H 'Content-Type: application/json' \
  -d '{
    "facts": {"income": 40000, "debt_ratio": 0.5},
    "target_conclusion": "approve",
    "candidates": {
      "income": [60000, 30000],
      "debt_ratio": [0.3, 0.7]
    },
    "max_changes": 2
  }'
```

响应（节选）：

```json
{
  "policy_id": "ab12...",
  "target_conclusion": "approve",
  "original_status": "no_match",
  "original_conclusion": null,
  "count": 1,
  "counterfactuals": [
    {
      "changes": {"debt_ratio": 0.3, "income": 60000},
      "verdict": {
        "status": "decided",
        "conclusion": "approve",
        "decided_priority": 10,
        "true_rule_ids": ["approve"],
        "level_trace": { "priority": 10, "state": "fired", "...": "..." },
        "levels_trace": [ "..." ]
      },
      "level_trace": { "priority": 10, "state": "fired", "...": "..." }
    }
  ]
}
```

搜索语义：

- 仅接受最终 `status` 为 `decided` 且结论等于 `target_conclusion` 的修改；
  修改后仍为 `unknown`、层内 `conflict` 或全部为假 `no_match` 的组合一律排除。
- **包含关系最小**针对“被修改的字段集合”：若某字段集已能达到目标，其任何超集
  都被剪枝；同一最小字段集上的不同取值若都能达到目标，则各自独立保留
  （它们互不包含）。
- 超过 `max_changes` 个字段的组合不会被枚举。
- 原始事实已经唯一得到目标结论时，直接返回**一个空修改集**
  （`"changes": {}`）。
- 结果稳定排序：先按修改字段数升序，再按字段名（`changes` 的键也按字段名
  排序输出），最后按候选值的规范 JSON 序（`json.dumps(..., sort_keys=True)`）。
- 为防止候选空间组合爆炸，求值组合数设有安全上限
  （`MAX_COUNTERFACTUAL_COMBINATIONS = 100000`）；触顶时返回 `422` 并提示
  收窄 `candidates` 或减小 `max_changes`，以保证结果始终完整。

## 批量数据方案示例（batch-data-plans）

一次请求提交多个案例，逐案复用裁决引擎；仅对裁决为 `unknown` 的案例，把
各自的 `minimal_missing_field_sets` 组合成**能让全部 unknown 案例同时可
判定**的所有**包含关系最小**全局字段集合（即数据补全方案）。每个方案列出
它覆盖的逐案最小集合；`decided` / `conflict` / `no_match` 案例不参与组合。

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `cases` | 非空数组 | 元素为 `{"case_id": <非空字符串>, "facts": <对象>}`；`facts` 缺省视为 `{}`（缺失/null 字段仍为 unknown） |

语义与约束：

- `case_id` 在请求内必须唯一，重复返回 `422`；`cases` 为空、元素不是对象、
  `facts` 非法等同样返回 `422`（响应体含 `errors` 列表）。
- 组合过程持续去重并剪除严格超集；生成的并集组合数超过
  `MAX_BATCH_PLAN_COMBINATIONS`（默认 `100000`，可用环境变量配置）时返回
  `422`，**不会截断**结果。
- 没有 `unknown` 案例时返回**一个空方案**：
  `"plans": [{"fields": [], "covers": {}}]`。
- 稳定排序：`cases` 按 `case_id`；`plans` 按字段数再按字段名；`covers`
  按 `case_id` 给出每个 unknown 案例被该方案覆盖的逐案最小集合。

```bash
curl -s -X POST localhost:8000/api/v1/policies/$PID/batch-data-plans \
  -H 'Content-Type: application/json' \
  -d '{
    "cases": [
      {"case_id": "applicant-1", "facts": {"income": 80000, "debt_ratio": 0.2}},
      {"case_id": "applicant-2", "facts": {"income": 80000}},
      {"case_id": "applicant-3", "facts": {}}
    ]
  }'
```

响应（节选）：

```json
{
  "policy_id": "ab12...",
  "case_count": 3,
  "unknown_count": 2,
  "cases": [
    { "case_id": "applicant-1", "status": "decided", "conclusion": "approve" },
    { "case_id": "applicant-2", "status": "unknown", "conclusion": null,
      "missing_fields": ["debt_ratio"],
      "minimal_missing_field_sets": [["debt_ratio"]] },
    { "case_id": "applicant-3", "status": "unknown", "conclusion": null,
      "missing_fields": ["debt_ratio"],
      "minimal_missing_field_sets": [["debt_ratio"]] }
  ],
  "count": 1,
  "plans": [
    { "fields": ["debt_ratio"],
      "covers": { "applicant-2": [["debt_ratio"]],
                  "applicant-3": [["debt_ratio"]] } }
  ]
}
```

即：只要补全 `debt_ratio`，两个 unknown 案例所在优先级层即可判定。若各
案例的最小集合互不相同，方案为逐案集合的并集组合（仍保持包含关系最小），
例如某案例需要 `age`、另一案例需要 `flag` 或 `vip` 时，方案为
`["age","flag"]` 与 `["age","vip"]` 两个。

## 替换影响预览示例（replacement-preview）

提交候选 `rules` 与一批带唯一 `case_id` 的案例，服务分别用**当前存储策略**
与**候选策略**逐案裁决，把每次裁决压缩成**业务结果签名**（`status`、
`conclusion`、`decided_priority`，以及 `conflict` 的
`conflicting_conclusions` / `unknown` 的 `minimal_missing_field_sets` 与
`pending_conclusions`），据此报告替换影响。**整个过程不写存储**，当前策略
保持不变。

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `rules` | 非空数组 | 候选策略规则（结构与创建策略相同，仅用于预览） |
| `cases` | 非空数组 | 元素为 `{"case_id": <非空字符串>, "facts": <对象>}`；`facts` 缺省视为 `{}` |

语义与约束：

- 仅**签名不同**的案例列入 `changes`（含 `before` / `after` 签名），按
  `case_id` 排序。
- `transition_counts` 把**全部**案例按“旧签名 → 新签名”汇总计数（未变化
  的案例体现为 `from == to`），按两个签名的规范 JSON 序确定性排序。
- 候选 `rules` 非法、`case_id` 重复、`cases` 为空等返回 `422`，`errors`
  同时列出规则与案例两侧的问题；此时同样不写入任何数据。

```bash
curl -s -X POST localhost:8000/api/v1/policies/$PID/replacement-preview \
  -H 'Content-Type: application/json' \
  -d '{
    "rules": [
      {"id": "approve", "priority": 10, "conclusion": "approve",
       "expr": {"all": [{"field": "income", "op": "ge", "value": 60000},
                        {"field": "debt_ratio", "op": "lt", "value": 0.4}]}},
      {"id": "reject", "priority": 10, "conclusion": "reject",
       "expr": {"field": "debt_ratio", "op": "ge", "value": 0.5}}
    ],
    "cases": [
      {"case_id": "applicant-1",
       "facts": {"income": 80000, "debt_ratio": 0.2}},
      {"case_id": "applicant-2",
       "facts": {"income": 55000, "debt_ratio": 0.3}},
      {"case_id": "applicant-3",
       "facts": {"income": 55000, "debt_ratio": 0.55}}
    ]
  }'
```

响应（节选；以上面“裁决示例”中创建的策略为当前策略）：

```json
{
  "policy_id": "ab12...",
  "case_count": 3,
  "change_count": 2,
  "changes": [
    { "case_id": "applicant-2",
      "before": { "status": "decided", "conclusion": "approve",
                  "decided_priority": 10 },
      "after":  { "status": "no_match", "conclusion": null,
                  "decided_priority": null } },
    { "case_id": "applicant-3",
      "before": { "status": "no_match", "conclusion": null,
                  "decided_priority": null },
      "after":  { "status": "decided", "conclusion": "reject",
                  "decided_priority": 10 } }
  ],
  "transition_counts": [
    { "from": { "status": "decided", "conclusion": "approve",
                "decided_priority": 10 },
      "to":   { "status": "decided", "conclusion": "approve",
                "decided_priority": 10 },
      "count": 1 },
    { "from": { "status": "decided", "conclusion": "approve",
                "decided_priority": 10 },
      "to":   { "status": "no_match", "conclusion": null,
                "decided_priority": null },
      "count": 1 },
    { "from": { "status": "no_match", "conclusion": null,
                "decided_priority": null },
      "to":   { "status": "decided", "conclusion": "reject",
                "decided_priority": 10 },
      "count": 1 }
  ]
}
```

即：把收入门槛从 50000 提到 60000、拒绝阈值从 0.6 降到 0.5 后，
`applicant-2` 从 approve 变为无匹配，`applicant-3` 从无匹配变为 reject，
`applicant-1` 不受影响；`transition_counts` 完整覆盖全部 3 个案例的迁移
分布。确认影响后如需真正替换策略，再调用 `PUT /api/v1/policies/{id}`。

## 裁决流示例（adjudication-flows/evaluate）

一次请求内联一张**节点有向图**：每个节点引用一条**已存策略**并声明路由 ——
按该节点的裁决结果（`decided` 的具体结论，或 `unknown` / `conflict` /
`no_match`）跳转到下一节点。从 `start` 出发逐节点复用裁决引擎（所有节点
共用同一份 `facts`），**当前节点的裁决没有匹配任何路由时流程结束**。
同一策略可被多个节点合法复用；整个过程只读取策略，**不修改策略存储**。

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `start` | 非空字符串 | 起始节点 id，必须在 `nodes` 中声明 |
| `nodes` | 非空数组 | 元素为 `{"id", "policy_id", "routes"}`；`id` 在请求内唯一，`policy_id` 必须引用已存策略，`routes` 可缺省（视为 `[]`） |
| `facts` | object | 全部节点共用的事实（缺省 `{}`，缺失/null 字段仍为 unknown） |

路由元素 `{"on", "conclusion", "next"}`：

- `on` ∈ `decided` / `unknown` / `conflict` / `no_match`；
- `conclusion` 仅允许且必须出现在 `decided` 路由上，匹配该具体结论；
- `next` 必须指向已声明的节点 id；
- 同一节点内匹配键（`on` + `conclusion`）重复返回 `422`。

执行前校验节点、路由与策略引用，并**仅对从 `start` 可达的子图**做环检测：
可达子图内有环（含自环）返回 `422`；不可达子图中的环不影响请求。校验
通过的有向图保证流程在节点数步内必然终止。

```bash
# 复用“裁决示例”创建的收入策略 $PID，再建一条 VIP 通道策略
P2=$(curl -s -X POST localhost:8000/api/v1/policies \
  -H 'Content-Type: application/json' \
  -d '{"rules":[
    {"id":"vip","priority":5,"conclusion":"fast-track",
     "expr":{"field":"vip","op":"eq","value":true}},
    {"id":"fallback","priority":1,"conclusion":"standard",
     "expr":{"field":"country","op":"eq","value":"NL"}}]}' \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

curl -s -X POST localhost:8000/api/v1/adjudication-flows/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "start": "income-check",
    "nodes": [
      {"id": "income-check", "policy_id": "'$PID'",
       "routes": [
         {"on": "decided", "conclusion": "approve", "next": "vip-lane"},
         {"on": "unknown", "next": "vip-lane"}
       ]},
      {"id": "vip-lane", "policy_id": "'$P2'", "routes": []}
    ],
    "facts": {"income": 80000, "debt_ratio": 0.2, "vip": true}
  }'
```

响应：

```json
{
  "termination_reason": "no_matching_route",
  "final_node_id": "vip-lane",
  "final_signature": { "status": "decided", "conclusion": "fast-track",
                       "decided_priority": 5 },
  "step_count": 2,
  "steps": [
    { "node_id": "income-check", "policy_id": "ab12...",
      "status": "decided", "conclusion": "approve", "decided_priority": 10,
      "route": { "on": "decided", "conclusion": "approve", "next": "vip-lane" } },
    { "node_id": "vip-lane", "policy_id": "cd34...",
      "status": "decided", "conclusion": "fast-track", "decided_priority": 5,
      "route": null }
  ]
}
```

- `termination_reason` 目前唯一取值为 `no_matching_route`（当前节点裁决
  没有匹配任何路由，流程结束）。
- `final_signature` 是终止节点裁决的**业务签名**（与替换影响预览相同：
  `status` / `conclusion` / `decided_priority`，`conflict` 附冲突结论，
  `unknown` 附最小缺失字段集合与待定结论）。
- `steps` 为**有序节点轨迹**：每个被执行节点一条，含该节点的裁决状态、
  结论与实际命中的路由（未命中为 `null`）。

## 项目结构

```
app/
  main.py            # FastAPI 路由、健康检查、请求模型
  validation.py      # 策略结构校验、反事实/批量/替换预览/裁决流请求校验
  logic.py           # 三值逻辑、最小决定族、比较可行性
  engine.py          # 优先级分层、冲突/未知/无匹配裁决、轨迹
  sources.py         # 多来源带优先级事实合并：最高优先级比较、冲突/null 不回填
  counterfactual.py  # 反事实最小修改集枚举、剪枝与稳定排序
  batch.py           # 批量数据方案：逐案裁决、全局最小字段集合组合
  preview.py         # 替换影响预览：业务签名、变化列表与迁移汇总
  flow.py            # 裁决流：逐节点裁决、按裁决结果路由与终止
  store.py           # 加锁的内存策略存储
tests/               # 纯标准库 runner，也可用 pytest
Dockerfile
docker-compose.yml
entrypoint.sh        # 启动时安装依赖并运行 uvicorn
```

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
# 或无需 pytest（tests 内置 runner）：
python3 tests/test_logic.py && python3 tests/test_engine.py && \
python3 tests/test_api.py && python3 tests/test_evaluate_sources.py && \
python3 tests/test_counterfactual.py && \
python3 tests/test_batch_data_plans.py && \
python3 tests/test_replacement_preview.py && python3 tests/test_flow.py
```
