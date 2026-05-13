# GUIDE-01 AnySQL 操作与维护手册

**生效日期**: 2026-05-13  
**状态**: 执行中

## 1. 启动方式

本地开发：

```powershell
python -m uvicorn anysql.main:app --host 127.0.0.1 --port 8765
```

团队版 Docker Compose：

```powershell
docker compose up -d --build
```

默认访问地址为 `http://127.0.0.1:8765`。生产数据位于 `deploy-data/`，重建容器不会删除历史数据。

## 2. 日常入口

- 首页 `/`: SQL Assistant 工作台。
- 产品 / 知识管理 `/products`: 产品配置、数据库连接、Metadata 同步、SQL 解析、表角色、RAG、Join Edge 管理。
- 旧分析页 `/analysis`: 仅作为兼容页面保留，不再作为主要操作入口。

右上角只保留“产品 / 知识管理”入口。SQL 解析、Metadata 同步、RAG 重建都在产品页完成。

## 3. 核心工作流

### 3.1 配置产品

1. 打开 `/products`。
2. 新增或编辑产品。
3. 填写 Code、名称、规则、目录和数据库连接资料。
4. 保存后，新产品会自动触发 Metadata 同步任务。

产品规则会作为 Harness Agent 的固定上下文限制，始终参与 SQL 生成。

### 3.2 同步 Metadata

在产品页点击 Metadata 同步按钮。后台会：

- 从产品数据库抽取 Metadata。
- 更新 `metadata_tables` / `metadata_columns`。
- 重建表角色 profile。
- 更新 metadata 向量。
- 生成多维 RAG 节点。

### 3.3 重建 RAG

在产品页 RAG 区点击重建按钮。系统会从 PostgreSQL 事实源重建：

- table profile / table semantic 节点。
- column semantic / column stat 节点。
- predicate pattern / business term 节点。
- 对应 pgvector embedding。

RAG 节点是可重建缓存，不是事实源。

### 3.4 SQL Assistant

1. 在首页选择产品。
2. 输入 SQL 需求。
3. 系统先拆解 intent，再准备 evidence bundle。
4. 大模型只根据受控上下文生成 SQL。
5. 校验不通过的 draft 不显示采纳按钮。
6. 人工采纳后，accepted SQL 会立即生成正反馈 RAG 节点并写入向量，下一轮请求可直接利用。

## 4. 测试数据清理

测试或 smoke test 后必须确认：

```sql
select count(*) from sql_drafts;
select count(*) from agent_runs;
select count(*) from agent_steps;
select count(*) from feedback_events;
```

测试产生的 draft、agent run、agent step 应清理。真实 Metadata、表角色 profile、accepted SQL 不应删除。

## 5. 常见问题

- RAG stats 为 0：说明尚未全量重建 RAG；assistant 会使用 legacy metadata/profile evidence fallback，但建议在产品页执行 RAG rebuild。
- SQL 解析按钮：只用于导入/分析原始 SQL 文件，不等同于 Metadata 同步。
- 采纳失败：通常是 draft 存在 validation warning，需先修正 SQL。
