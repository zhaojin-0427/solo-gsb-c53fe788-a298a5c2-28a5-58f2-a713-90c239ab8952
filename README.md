# 多材料 3D 打印换料规划 API

使用 Python + FastAPI + Google OR-Tools CP-SAT 实现的单打印机排程服务。服务在层段边界交错执行多个作业，并按照材料切换矩阵计算 purge 体积、切换耗时、容量余量和各作业逾期。

## 优化规则

请求必须包含 2～8 个作业，每个作业包含一个或多个按顺序打印的层段。模型假设：

1. 同一台打印机一次只打印一个层段，不能在层段中途切换作业。
2. 每个作业必须严格按照 `layers` 数组中的层序执行。
3. 当前材料（第一个层段前为初始材料）与下一层段材料不同时：
   - 在该层段开始前消耗 `purge_matrix[from][to]`；
   - 在该层段开始前增加 `change_seconds` 秒切换时间。
4. 材料相同时不产生 purge，也不消耗切换时间。
5. 作业完成时间等于该作业最后一个层段的结束时间；逾期为 `max(0, completion_seconds - deadline_seconds)`。
6. 层段之间不主动插入空转；空转不会改善 purge 或逾期。若第一个层段需要换料，它的 `start_seconds` 为切换耗时。
7. 总 purge 体积超过 `total_purge_capacity` 时无解。

可行计划按以下固定优先级选择：

1. 最小化总 purge 体积；
2. 在 purge 最优的计划中最小化全部作业逾期秒数之和；
3. 仍相同时，按层段键 `(job_id, layer_order)` 排序，取全局顺序的字典序最小计划。

CP-SAT 在 10 秒总时限内求解。只有所有上述层级均证明最优时才返回 `OPTIMAL`；只找到可行解但未证明最优时返回 `FEASIBLE`。

## 一键启动

要求本机可用 Docker 与 Docker Compose v2。

```bash
docker compose up --build
```

服务启动后访问：

- 健康检查：<http://localhost:8000/health>
- Swagger UI：<http://localhost:8000/docs>
- OpenAPI JSON：<http://localhost:8000/openapi.json>
- 规划接口：`POST http://localhost:8000/plans`

停止服务：

```bash
docker compose down
```

## 本地非 Docker 运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 请求格式

顶层字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `jobs` | array，2～8 项 | 待打印作业 |
| `initial_material_id` | string | 打印机初始材料 |
| `purge_matrix` | object | 二维 purge 矩阵，行列都必须覆盖初始材料和所有层段材料 |
| `change_seconds` | non-negative integer | 每次材料切换耗时，单位秒 |
| `total_purge_capacity` | non-negative integer | 最大总 purge 容量 |

每个作业：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | non-empty string | 唯一作业 ID |
| `layers` | non-empty array | 按顺序打印的层段 |
| `deadline_seconds` | non-negative integer | 截止秒数 |

每个层段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `material_id` | non-empty string | 材料 ID |
| `print_seconds` | positive integer | 打印秒数 |
| `volume` | positive integer | 该层段材料体积，作为计划明细返回 |

所有时间和体积字段必须是整数；布尔值、负数、未知字段、重复作业 ID、缺失或多余的 purge 矩阵行列都会返回 HTTP 422。相同材料对应的矩阵对角线必须为 0。

## 示例请求

仓库中的 `examples/request.json` 与以下内容一致：

```json
{
  "jobs": [
    {
      "id": "J1",
      "layers": [
        {"material_id": "A", "print_seconds": 30, "volume": 10},
        {"material_id": "B", "print_seconds": 20, "volume": 8},
        {"material_id": "A", "print_seconds": 40, "volume": 12}
      ],
      "deadline_seconds": 80
    },
    {
      "id": "J2",
      "layers": [
        {"material_id": "A", "print_seconds": 25, "volume": 9},
        {"material_id": "B", "print_seconds": 35, "volume": 11}
      ],
      "deadline_seconds": 110
    }
  ],
  "initial_material_id": "A",
  "purge_matrix": {
    "A": {"A": 0, "B": 20},
    "B": {"A": 15, "B": 0}
  },
  "change_seconds": 10,
  "total_purge_capacity": 100
}
```

调用：

```bash
curl -s http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  --data @examples/request.json
```

## 与当前实现一致的成功响应

下面是上述请求由本服务返回的最优计划。两次材料切换消耗 `20 + 15 = 35` purge，容量余量为 `100 - 35 = 65`，总逾期为 90 秒。

```json
{
  "status": "OPTIMAL",
  "reason_code": "OPTIMAL",
  "message": "Found the purge-minimal plan with minimum tardiness among those plans and the lexicographically smallest deterministic schedule.",
  "objective": {
    "total_purge_volume": 35,
    "remaining_purge_capacity": 65,
    "total_tardiness_seconds": 90
  },
  "plan": [
    {
      "job_id": "J1",
      "layer_order": 1,
      "material_id": "A",
      "start_seconds": 0,
      "end_seconds": 30,
      "print_seconds": 30,
      "volume": 10,
      "rank": 1
    },
    {
      "job_id": "J2",
      "layer_order": 1,
      "material_id": "A",
      "start_seconds": 30,
      "end_seconds": 55,
      "print_seconds": 25,
      "volume": 9,
      "rank": 2
    },
    {
      "job_id": "J2",
      "layer_order": 2,
      "material_id": "B",
      "start_seconds": 65,
      "end_seconds": 100,
      "print_seconds": 35,
      "volume": 11,
      "rank": 3
    },
    {
      "job_id": "J1",
      "layer_order": 2,
      "material_id": "B",
      "start_seconds": 100,
      "end_seconds": 120,
      "print_seconds": 20,
      "volume": 8,
      "rank": 4
    },
    {
      "job_id": "J1",
      "layer_order": 3,
      "material_id": "A",
      "start_seconds": 130,
      "end_seconds": 170,
      "print_seconds": 40,
      "volume": 12,
      "rank": 5
    }
  ],
  "switches": [
    {
      "from_material_id": "A",
      "to_material_id": "B",
      "before_rank": 3,
      "start_seconds": 55,
      "duration_seconds": 10,
      "purge_volume": 20
    },
    {
      "from_material_id": "B",
      "to_material_id": "A",
      "before_rank": 5,
      "start_seconds": 120,
      "duration_seconds": 10,
      "purge_volume": 15
    }
  ],
  "tardiness": [
    {
      "job_id": "J1",
      "deadline_seconds": 80,
      "completion_seconds": 170,
      "tardy_seconds": 90
    },
    {
      "job_id": "J2",
      "deadline_seconds": 110,
      "completion_seconds": 100,
      "tardy_seconds": 0
    }
  ]
}
```

## 无解和求解状态

HTTP 状态码为 200 时，业务状态在响应的 `status` 中：

- `OPTIMAL`：purge、逾期和字典序三级目标均已证明最优；
- `FEASIBLE`：找到可行计划，但至少一个优化层级未在时限内证明最优，因此不会标记为最优；
- `INFEASIBLE`：没有可行计划；
- `UNKNOWN`：求解器在时限内未能确定可行性。

`INFEASIBLE.reason_code` 可能为：

- `PURGE_CAPACITY_EXCEEDED`：即使放宽层序约束，purge 容量仍不足；
- `PRECEDENCE_CONFLICT`：容量内存在材料序列，但无法同时满足所有作业的层序约束；
- `INFEASIBILITY_CAUSE_UNDETERMINED`：诊断模型也在时限内无法确定原因。

请求本身不合法时由 FastAPI/Pydantic 返回 HTTP 422，响应体包含字段级错误明细。
