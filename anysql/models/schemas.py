"""
AnySQL 数据模型

定义所有核心数据结构（Pydantic 模型）。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 枚举类型
# ---------------------------------------------------------------------------

class StatementType(str, Enum):
    """SQL 语句类型"""
    SELECT = "SELECT"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    CREATE = "CREATE"
    ALTER = "ALTER"
    DROP = "DROP"
    MERGE = "MERGE"
    OTHER = "OTHER"


class AnalysisStatus(str, Enum):
    """分析状态"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    COMPLETED = "completed"


class Complexity(str, Enum):
    """复杂度评级"""
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


# ---------------------------------------------------------------------------
# SQL 语句模型
# ---------------------------------------------------------------------------

class SQLStatement(BaseModel):
    """解析后的 SQL 语句"""
    id: str = Field(description="唯一ID: {product}_{file_stem}_{index}")
    product: str = Field(description="产品ID")
    source_file: str = Field(description="来源文件名")
    raw_sql: str = Field(description="原始SQL文本")
    comment: str = Field(default="", description="前导注释")
    tables: list[str] = Field(default_factory=list, description="引用的表名")
    statement_type: StatementType = Field(default=StatementType.OTHER)
    line_number: int = Field(default=0, description="在文件中的行号")


# ---------------------------------------------------------------------------
# LLM 分析结果模型
# ---------------------------------------------------------------------------

class SQLAnalysis(BaseModel):
    """LLM 对 SQL 的分析结果"""
    summary: str = Field(description="一句话总结")
    business_context: list[str] = Field(
        default_factory=list, description="可能的业务场景"
    )
    tables_involved: dict[str, str] = Field(
        default_factory=dict, description="涉及的表及其含义"
    )
    usage_guide: str = Field(default="", description="使用方法说明")
    category: list[str] = Field(
        default_factory=list, description="分类标签"
    )
    complexity: Complexity = Field(default=Complexity.SIMPLE)
    keywords: list[str] = Field(
        default_factory=list, description="关键词"
    )
    parameters: list[str] = Field(
        default_factory=list, description="需要替换的参数说明"
    )


class SQLRecord(BaseModel):
    """完整的 SQL 记录（语句 + 分析）"""
    statement: SQLStatement
    analysis: Optional[SQLAnalysis] = None
    status: AnalysisStatus = AnalysisStatus.PENDING
    analyzed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    metadata_snapshot: dict[str, dict] = Field(
        default_factory=dict,
        description="分析时捕获的相关表元数据快照",
    )


# ---------------------------------------------------------------------------
# 搜索相关模型
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    """搜索请求"""
    query: str = Field(description="自然语言查询")
    product: Optional[str] = Field(default=None, description="限定产品")
    top_k: int = Field(default=10, ge=1, le=50, description="返回数量")


class SearchResult(BaseModel):
    """单条搜索结果"""
    sql_id: str
    score: float = Field(description="相关性评分 0-1")
    summary: str
    raw_sql: str
    comment: str
    source_file: str
    product: str
    category: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    business_context: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    """搜索响应"""
    query: str
    results: list[SearchResult]
    total: int
    elapsed_ms: float


# ---------------------------------------------------------------------------
# SQL 生成与知识沉淀模型
# ---------------------------------------------------------------------------

class SQLGenerationRequest(BaseModel):
    """基于产品知识生成 SQL 的请求"""
    product: str = Field(description="目标产品ID")
    requirement: str = Field(description="自然语言业务需求")
    top_k: int = Field(default=5, ge=1, le=20, description="参考相似SQL数量")


class GeneratedSQL(BaseModel):
    """LLM 生成的 SQL 及解释"""
    sql: str = Field(description="可执行或可改造的SQL")
    summary: str = Field(description="SQL用途总结")
    business_meaning: str = Field(default="", description="业务含义")
    usage_guide: str = Field(default="", description="参数替换和使用方法")
    parameters: list[str] = Field(default_factory=list, description="参数说明")
    tables: list[str] = Field(default_factory=list, description="涉及表名")
    assumptions: list[str] = Field(default_factory=list, description="生成时的假设")


class SQLGenerationResponse(BaseModel):
    """SQL 生成响应，默认返回待人工确认的草稿记录"""
    product: str
    requirement: str
    generated: GeneratedSQL
    record: SQLRecord
    learned: bool = False


class SQLCandidateMatch(BaseModel):
    """候选 SQL 的模型匹配评估"""
    sql_id: str
    vector_score: float = Field(ge=0, le=1)
    llm_score: float = Field(ge=0, le=1)
    reason: str = ""


class SQLAssistantRequest(BaseModel):
    """SQL 辅助对话请求"""
    product: str = Field(description="目标产品ID")
    message: str = Field(description="用户需求或修正意见")
    session_id: Optional[str] = Field(default=None, description="前端会话ID")
    current_sql_id: Optional[str] = Field(default=None, description="当前正在修正的SQL ID")
    current_sql: Optional[str] = Field(default=None, description="当前正在修正的SQL文本")
    top_k: int = Field(default=5, ge=1, le=20)
    match_threshold: float = Field(default=0.78, ge=0, le=1)
    use_aliases: bool = Field(default=False, description="是否允许生成列别名")


class SQLAssistantResponse(BaseModel):
    """SQL 辅助对话响应"""
    product: str
    session_id: Optional[str] = None
    mode: str = Field(description="matched/generated/revised")
    message: str
    matches: list[SQLCandidateMatch] = Field(default_factory=list)
    record: SQLRecord
    learned: bool = False
    agent_run_id: Optional[str] = None
    intent_plan: dict = Field(default_factory=dict)
    evidence_bundles: list[dict] = Field(default_factory=list)
    context_budget: dict = Field(default_factory=dict)
    retrieval_scores: list[dict] = Field(default_factory=list)
    validation_result: dict = Field(default_factory=dict)
    repair_count: int = 0
    llm_call_count: int = 0
    invalid_reason: str = ""


class SQLLearnRequest(BaseModel):
    """人工确认后将当前 SQL 草稿纳入知识库"""
    product: str
    requirement: str
    sql: str
    summary: str = ""
    business_meaning: str = ""
    usage_guide: str = ""
    parameters: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 产品与分析状态模型
# ---------------------------------------------------------------------------

class ProductInfo(BaseModel):
    """产品信息"""
    id: str
    physical_id: str = ""
    code: str = ""
    name: str
    description: str = ""
    rules: str = ""
    sql_dir: str = ""
    desc_dir: str = ""
    metadata_dir: str = ""
    database_type: str = "oracle"
    database_host: Optional[str] = None
    database_port: int = 1521
    database_service_name: Optional[str] = None
    database_username: Optional[str] = None
    database_password: Optional[str] = None
    sql_count: int = 0
    analyzed_count: int = 0
    db_configured: bool = False


class ProductUpsertRequest(BaseModel):
    """新增或更新产品配置"""
    physical_id: Optional[str] = None
    code: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    name: str
    description: str = ""
    rules: str = ""
    sql_dir: str
    desc_dir: str
    metadata_dir: str
    database_type: str = "oracle"
    database_host: Optional[str] = None
    database_port: int = 1521
    database_service_name: Optional[str] = None
    database_username: Optional[str] = None
    database_password: Optional[str] = None


class AnalysisProgress(BaseModel):
    """分析进度"""
    product: str
    total: int = 0
    completed: int = 0
    failed: int = 0
    current_sql: Optional[str] = None
    status: AnalysisStatus = AnalysisStatus.PENDING
    started_at: Optional[datetime] = None
    elapsed_seconds: float = 0
    errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent 相关模型
# ---------------------------------------------------------------------------

class DiagnosisResult(BaseModel):
    """Agent 错误诊断结果"""
    error_type: str
    root_cause: str
    suggested_fix: str
    confidence: float = Field(ge=0, le=1)
    retry_recommended: bool = True


class FixAction(BaseModel):
    """Agent 修正动作"""
    action_type: str  # retry, modify_prompt, truncate, skip
    modified_prompt: Optional[str] = None
    reason: str = ""
