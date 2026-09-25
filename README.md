# 传染病暴发调查与接触网络

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8303`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8303
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `case`：病例和调查状态；`contact`：接触者随访。
- `batch`：检验批次（`open → closed`，有待处理样本时不能关闭）。
- `sample`：送检样本，按批次和病例关联，状态机：
  - `received → review_pending → reviewed_positive / reviewed_negative`
  - 初筛阴性：`received → screened_negative`
  - 样本失效：`received / review_pending → invalid`，失效后通过重采生成新样本
- 检验结果写回规则：
  - 初筛阳性只进入复核，不改变病例；复核阳性才确认病例（`investigating/probable → confirmed`）。
  - 复核阴性或样本失效时，病例保持原诊断。
  - 同一病例重复送检时以最新有效结果为准（失效样本不算有效结果）。
  - 病例侧的 `lab_positive` 动作必须引用该病例最新有效的复核阳性样本。
  - 没有样本记录的旧病例，检验状态按"待送检"处理。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤（支持 `cases`、`contacts`、`batches`、`samples`）。
- `POST /api/<kind>`：创建对象；请求体为JSON。样本需提供 `case_id`、`batch_id`，重采样本带 `recollected_from`。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/samples/<id>/recollect`：对失效样本发起重采，返回新样本。
- `GET /api/batches/<id>/progress`：批次进度（总数、各状态计数、待处理数、复核阳性数）。
- `GET /api/lab/board`：检验室看板（待处理/待复核/失效样本数、待送检病例数、批次进度、每个病例的派生检验状态）。
- `GET /api/audit`：读取审计记录。

### 检验动作一览

| 对象 | 动作 | 角色 |
| --- | --- | --- |
| sample | `screen_positive` / `screen_negative`（初筛） | lab |
| sample | `review_positive` / `review_negative`（复核） | lab |
| sample | `mark_invalid`（需 `reason`） | lab |
| batch | `close` | lab |

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
