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
- `batch`：检验批次；`sample`：样本，同时关联`batch_id`和`case_id`，重采样本用`resample_of`关联原失效样本。

## 检验流程

样本状态机：`collected`（待初筛）→ 初筛阳性进入`pending_review`（待复核），初筛阴性直接判`screened_negative`；复核阳性判`confirmed_positive`，复核阴性判`review_negative`；失效样本判`invalid`后可重采。

- 复核阳性才确认病例（`reported`/`investigating`→`confirmed`），初筛阳性不直接改写诊断。
- 复核阴性或样本失效时，病例保持原诊断。
- 同一病例重复送检时，病例采纳最新有效结果（`lab_result`），失效样本不参与。
- 没有样本记录的旧病例，检验视图为`pending_submission`（待送检）。
- 批次尚有待处理样本时不能关闭（`close_batch`）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/batches/<id>/progress`：批次进度、各状态样本数和待处理样本数。
- `GET /api/cases/<id>/lab`：病例的检验视图（待送检/待初筛/待复核/待重采/阴性/阳性）。
- `GET /api/lab/pending`：检验科待办汇总（待初筛、待复核，按批次分组）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
