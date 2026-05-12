# ARCH-01 AnySQL 稳健性架构设计

**生效日期**: 2026-05-12  
**状态**: 执行中 (Active)

---

## 1. 架构目标
本架构旨在构建一个高容错、低延迟的 SQL 语义分析平台，特别强调对多语言（中/日/英）环境的友好支持以及分析过程的透明度。

## 2. 核心组件设计

### 2.1 状态管理逻辑 (State Sync)
为了解决异步解析过程中的进度同步问题，系统采用了“单例内存映射”机制：
- **逻辑容器**: `AnalysisPipeline` 维护一个 `_progress_map`。
- **一致性保证**: 无论是 Web API 请求还是 FastAPI 的 `BackgroundTasks` 线程，均通过 `get_progress()` 访问同一个 `AnalysisProgress` 对象。
- **降级方案**: 系统支持从磁盘 JSON 文件数自动恢复内存状态，确保服务器重启后进度不丢失。

### 2.2 Metadata 驱动分析 (Metadata-Driven)
系统不再孤立地分析 SQL 字符串，而是通过 `MetadataCollector` 建立关联：
- **上下文注入**: 在发送给 AI (Agent) 的 Prompt 中，系统会自动提取该 SQL 涉及的所有表的 DDL、注释以及字段描述。
- **分析深度**: AI 能够基于表结构的业务语义（如：`DHJKIDO` 表代表异动记录）给出更准确的业务逻辑摘要。

### 2.3 全链路 UTF-8 标准
- **输入层**: SQL 文件强制探测编码。
- **处理层**: Python 内部字符串全量统一。
- **持久化层**: `json.dump` 禁用 ASCII 转换，存储原始多字节字符。
- **展现层**: HTML Meta 标签声明 UTF-8。

## 3. 数据模型规范 (Logical Schema)

| 实体 | 字段 | 逻辑类型 | 说明 |
| :--- | :--- | :--- | :--- |
| **SQLRecord** | `statement` | Object | 包含原始 SQL、文件名、ID |
| | `analysis` | Object | AI 生成的语义分析结果 |
| | `metadata_snapshot` | Map | 分析时的表结构快照 |
| **SQLAnalysis** | `summary` | String | 业务逻辑摘要 (日文) |
| | `category` | List | 业务领域分类 (如 人事, 薪资) |
| | `business_context` | List | 利用场景列表 |

## 4. 接口规范
- **搜索**: `GET /api/search?q={query}` -> 返回向量加权后的结果列表。
- **进度**: `GET /api/analysis/{product}/progress` -> 返回实时解析计数。

## 5. 安全与稳健性
- **URL 安全**: 所有包含多字节字符的 ID 在传递前必须经过 URL 编码，并在接收端进行 `unquote` 解码。
- **容错渲染**: 前端模板对 `None` 值进行保护，确保部分数据缺失时页面不崩溃。
