# ARCH-01 AnySQL 稳健性架构设计

**生效日期**: 2026-05-13  
**状态**: 执行中

## 1. 目标

AnySQL 的稳健性目标已经从“单次语义匹配”升级为“准备工作优先的 Harness Agent”：

- 大模型不负责猜测全局。
- 系统先完成 intent 拆解、规则约束、多维召回、证据融合和上下文组装。
- 大模型只在受控 evidence bundle 内生成或修正 SQL。
- 生成结果必须经过确定性 verifier。
- 正反馈必须即时沉淀到下一轮检索。

## 2. 核心组件

### 2.1 PostgreSQL 事实源

PostgreSQL 保存产品、连接、Metadata、SQL 知识、草稿、采纳记录、任务、RAG 节点和 Agent 审计记录。

文件系统只用于导入、导出和诊断，不是团队版知识事实源。

### 2.2 多维 RAG

系统不再只依赖 SQL 整体向量和表级 Metadata 向量，而是生成多种节点：

- `table_profile_node`
- `table_semantic_node`
- `column_semantic_node`
- `column_stat_node`
- `sql_intent_node`
- `sql_structure_node`
- `predicate_pattern_node`
- `business_term_node`
- `feedback_node`

节点写入 `rag_nodes`，向量写入 `rag_embeddings`。节点可从事实源重建。

### 2.3 Harness Agent

Assistant 请求会产生 `agent_runs` 和 `agent_steps`：

1. `IntentPlanner`: 保留用户条件、目标字段、业务域和表角色限制。
2. `KnowledgePreparer`: 检索表、字段、SQL、predicate、feedback evidence。
3. `EvidenceRanker`: 融合多路证据，避免单一向量分数带偏。
4. `ContextBuilder`: 组装大上下文，但只包含高置信证据。
5. `SQLComposer`: 调用 LLM 生成结构化 SQL draft。
6. `VerifierAndRepair`: 检查表/字段/条件漂移，必要时修复。

### 2.4 正反馈沉淀

`POST /api/assistant/learn` 是知识入库边界。采纳后同步完成：

- accepted SQL 写入 `sql_records`。
- acceptance 写入 `knowledge_acceptances`。
- SQL intent / structure / predicate / feedback 节点写入 RAG。
- pgvector embedding 立即更新。

未采纳 draft、invalid draft 和测试结果不能成为正样本。

## 3. UI 入口策略

顶栏只保留：

- 首页 SQL Assistant。
- 产品 / 知识管理。
- 语言选择。

旧分析面板 `/analysis` 保留兼容，但不再作为主要入口。SQL 解析按钮已经合并到产品页。

## 4. API

主要接口：

- `POST /api/assistant/sql`
- `POST /api/assistant/learn`
- `GET /api/agent-runs/{run_id}`
- `POST /api/products/{product}/rag/rebuild`
- `GET /api/products/{product}/rag/stats`
- `GET /api/products/{product}/join-edges`
- `PATCH /api/products/{product}/join-edges/{edge_id}`
- `POST /api/analysis/{product}/metadata-sync`
- `POST /api/analysis/{product}/start`

## 5. 稳健性规则

- 普通业务查询不得使用 log/work/if/backup 表，除非 intent 明确允许。
- 多表 SQL 必须有 join edge 或已采纳 SQL 结构证据。
- 用户条件不能丢失，系统不得新增无证据条件。
- 参数名必须 ASCII。
- invalid draft 不显示采纳按钮。
- 测试数据必须在测试后清理：`sql_drafts`、`agent_runs`、`agent_steps`、测试 feedback。
