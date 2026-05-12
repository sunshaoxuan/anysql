"""
AnySQL Agent 引擎 — 完整自改善框架

基于 Hermes-style Agent 生态的完整实现：
- 状态机驱动的任务执行
- 工具注册表（Tool Registry）
- 反思-诊断-修正 循环
- 执行历史与经验记忆
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

from pydantic import BaseModel

from anysql.logger import logger


# ---------------------------------------------------------------------------
# Agent 状态机
# ---------------------------------------------------------------------------

class AgentState(str, Enum):
    """Agent 执行状态"""
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    REFLECTING = "reflecting"
    DIAGNOSING = "diagnosing"
    FIXING = "fixing"
    COMPLETED = "completed"
    FAILED = "failed"


class ToolResultStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """工具执行结果"""
    status: ToolResultStatus
    data: Any = None
    error: Optional[str] = None
    elapsed_ms: float = 0


@dataclass
class AgentStep:
    """Agent 执行的单步记录"""
    step_id: int
    action: str  # tool name or internal action
    input_summary: str
    result: Optional[ToolResult] = None
    state: AgentState = AgentState.IDLE
    timestamp: float = field(default_factory=time.time)
    reflection: Optional[str] = None


@dataclass
class ExperienceEntry:
    """经验记录（成功/失败的模式）"""
    error_pattern: str
    fix_applied: str
    success: bool
    context: str = ""
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

# 工具类型定义
ToolFunc = Callable[..., Coroutine[Any, Any, ToolResult]]


@dataclass
class ToolDefinition:
    """工具定义"""
    name: str
    description: str
    func: ToolFunc
    category: str = "general"


class ToolRegistry:
    """工具注册表"""

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        name: str,
        description: str,
        func: ToolFunc,
        category: str = "general",
    ) -> None:
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            func=func,
            category=category,
        )
        logger.debug(f"Agent 工具已注册: {name} ({category})")

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def list_tools(self) -> list[dict[str, str]]:
        return [
            {"name": t.name, "description": t.description, "category": t.category}
            for t in self._tools.values()
        ]

    def get_tools_prompt(self) -> str:
        """生成供 LLM 使用的工具描述"""
        lines = ["可用工具:"]
        for t in self._tools.values():
            lines.append(f"- {t.name}: {t.description}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent Engine
# ---------------------------------------------------------------------------

class AgentEngine:
    """
    完整的 Agent 自改善引擎。

    核心循环: Plan → Execute → Reflect → (Diagnose → Fix → Retry)

    特性:
    - 状态机驱动，每步可追踪
    - 工具注册表，可动态扩展
    - 经验记忆，避免重复错误
    - LLM 驱动的反思与诊断
    """

    def __init__(self, llm_client, max_retries: int = 3):
        self.llm = llm_client
        self.max_retries = max_retries
        self.tools = ToolRegistry()
        self.state = AgentState.IDLE
        self.steps: list[AgentStep] = []
        self.experience: list[ExperienceEntry] = []
        self._step_counter = 0

        logger.info("AgentEngine 初始化完成")

    async def execute_task(
        self,
        prompt: str,
        response_model: type[BaseModel],
        system_prompt: str = "",
    ) -> BaseModel:
        """
        直接执行一次结构化 LLM 任务，并校验为指定 Pydantic 模型。
        """
        schema = response_model.model_json_schema()
        full_prompt = f"""{prompt}

请只返回符合以下 JSON Schema 的 JSON 对象，不要包含 Markdown 代码块或额外解释:
{json.dumps(schema, ensure_ascii=False)}
"""
        data = await self.llm.chat_json(
            prompt=full_prompt,
            system_prompt=system_prompt or "你是 SQL 分析助手，请返回严格 JSON。",
            temperature=0.1,
        )
        if "_parse_error" in data:
            raise ValueError(f"LLM 返回内容不是有效 JSON: {data.get('_parse_error')}")
        return response_model.model_validate(data)

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------

    def _transition(self, new_state: AgentState) -> None:
        old = self.state
        self.state = new_state
        logger.debug(f"Agent 状态转换: {old.value} → {new_state.value}")

    def _new_step(self, action: str, input_summary: str) -> AgentStep:
        self._step_counter += 1
        step = AgentStep(
            step_id=self._step_counter,
            action=action,
            input_summary=input_summary,
            state=self.state,
        )
        self.steps.append(step)
        return step

    # ------------------------------------------------------------------
    # 核心执行
    # ------------------------------------------------------------------

    async def execute_with_healing(
        self,
        task_name: str,
        task_func: Callable[..., Coroutine],
        *args,
        context: str = "",
        **kwargs,
    ) -> ToolResult:
        """
        执行任务，失败时自动进入 反思→诊断→修正→重试 循环。

        Args:
            task_name: 任务名称
            task_func: 异步任务函数
            context: 上下文信息
            *args, **kwargs: 传递给 task_func 的参数

        Returns:
            ToolResult: 最终执行结果
        """
        logger.info(f"Agent 开始任务: {task_name}")
        self._transition(AgentState.EXECUTING)

        last_error: Optional[str] = None
        modified_kwargs = dict(kwargs)

        for attempt in range(1, self.max_retries + 1):
            step = self._new_step(task_name, f"attempt={attempt}")
            t0 = time.time()

            try:
                result = await task_func(*args, **modified_kwargs)
                elapsed = (time.time() - t0) * 1000

                step.result = ToolResult(
                    status=ToolResultStatus.SUCCESS,
                    data=result,
                    elapsed_ms=elapsed,
                )
                step.state = AgentState.COMPLETED
                self._transition(AgentState.COMPLETED)

                logger.info(
                    f"Agent 任务成功: {task_name} "
                    f"(attempt {attempt}, {elapsed:.0f}ms)"
                )
                return step.result

            except Exception as e:
                elapsed = (time.time() - t0) * 1000
                error_info = f"{type(e).__name__}: {e}"
                tb = traceback.format_exc()

                step.result = ToolResult(
                    status=ToolResultStatus.FAILURE,
                    error=error_info,
                    elapsed_ms=elapsed,
                )

                logger.warning(
                    f"Agent 任务失败 (attempt {attempt}): "
                    f"{task_name} — {error_info}"
                )

                if attempt < self.max_retries:
                    # 进入反思循环
                    fix_result = await self._healing_cycle(
                        task_name=task_name,
                        error=error_info,
                        traceback_str=tb,
                        context=context,
                        attempt=attempt,
                        previous_error=last_error,
                    )

                    if fix_result and fix_result.get("modified_kwargs"):
                        modified_kwargs.update(fix_result["modified_kwargs"])
                        logger.info(
                            f"Agent 应用修正: {fix_result.get('fix_description', '?')}"
                        )

                    last_error = error_info
                else:
                    self._transition(AgentState.FAILED)
                    logger.error(
                        f"Agent 任务最终失败: {task_name}, "
                        f"共尝试 {attempt} 次"
                    )
                    return step.result

        # 不应到达
        return ToolResult(status=ToolResultStatus.FAILURE, error="max retries exceeded")

    # ------------------------------------------------------------------
    # 反思-诊断-修正 循环
    # ------------------------------------------------------------------

    async def _healing_cycle(
        self,
        task_name: str,
        error: str,
        traceback_str: str,
        context: str,
        attempt: int,
        previous_error: Optional[str],
    ) -> Optional[dict]:
        """
        完整的自改善循环：

        1. Reflect（反思）：分析错误本质
        2. Diagnose（诊断）：找出根因
        3. Fix（修正）：生成具体修正方案
        """

        # --- Step 1: Reflect ---
        self._transition(AgentState.REFLECTING)
        reflection = await self._reflect(task_name, error, context, previous_error)

        step = self._new_step("reflect", f"error={error[:80]}")
        step.reflection = reflection
        step.state = AgentState.REFLECTING

        # --- Step 2: Diagnose ---
        self._transition(AgentState.DIAGNOSING)
        diagnosis = await self._diagnose(
            task_name, error, traceback_str, reflection, context
        )

        step = self._new_step("diagnose", f"reflection={reflection[:80]}")
        step.result = ToolResult(
            status=ToolResultStatus.SUCCESS,
            data=diagnosis,
        )

        # --- Step 3: Fix ---
        self._transition(AgentState.FIXING)
        fix = await self._generate_fix(diagnosis, context)

        step = self._new_step("fix", f"diagnosis={json.dumps(diagnosis, ensure_ascii=False)[:80]}")
        step.result = ToolResult(
            status=ToolResultStatus.SUCCESS,
            data=fix,
        )

        # 记录经验
        self.experience.append(ExperienceEntry(
            error_pattern=error,
            fix_applied=fix.get("fix_description", "unknown"),
            success=False,  # 会在重试成功后更新
            context=task_name,
        ))

        self._transition(AgentState.EXECUTING)
        return fix

    async def _reflect(
        self,
        task_name: str,
        error: str,
        context: str,
        previous_error: Optional[str],
    ) -> str:
        """反思：分析错误的本质和模式"""
        # 检查经验记忆中是否有类似错误
        similar_exp = [
            e for e in self.experience
            if e.error_pattern.split(":")[0] == error.split(":")[0]
        ]

        exp_info = ""
        if similar_exp:
            exp_info = (
                f"\n\n## 历史经验\n"
                f"之前遇到过 {len(similar_exp)} 次类似错误:\n"
            )
            for e in similar_exp[-3:]:
                exp_info += f"- 修正: {e.fix_applied}, 成功: {e.success}\n"

        prompt = f"""你是一个错误分析专家。请反思以下任务执行失败的原因。

## 任务
{task_name}

## 上下文
{context}

## 错误信息
{error}

## 上次错误
{previous_error or "无（首次尝试）"}
{exp_info}

请用简洁的一段话分析错误的本质，不要重复错误信息本身。
Focus on: 为什么会发生？这是暂时性的还是系统性的？"""

        try:
            reflection = await self.llm.chat(
                prompt=prompt,
                system_prompt="你是一个系统错误分析专家，请用中文简洁回答。",
                temperature=0.2,
            )
            logger.info(f"Agent 反思完成: {reflection[:100]}")
            return reflection.strip()
        except Exception as e:
            logger.warning(f"Agent 反思调用失败: {e}")
            return f"反思失败: {error}"

    async def _diagnose(
        self,
        task_name: str,
        error: str,
        traceback_str: str,
        reflection: str,
        context: str,
    ) -> dict:
        """诊断：找出根因并给出分类"""
        prompt = f"""你是一个系统诊断专家。根据以下信息诊断问题根因。

## 任务: {task_name}
## 反思结论: {reflection}
## 错误: {error}
## 堆栈追踪（前500字符）:
{traceback_str[:500]}

请以JSON格式返回诊断结果:
{{
    "error_type": "网络/解析/超时/配置/数据/模型/未知",
    "root_cause": "根因描述",
    "severity": "low/medium/high/critical",
    "retry_recommended": true/false,
    "fix_strategy": "具体修正策略描述"
}}"""

        try:
            result = await self.llm.chat_json(
                prompt=prompt,
                system_prompt="你是系统诊断专家，请返回纯JSON。",
                temperature=0.1,
            )
            logger.info(
                f"Agent 诊断完成: type={result.get('error_type')}, "
                f"retry={result.get('retry_recommended')}"
            )
            return result
        except Exception as e:
            logger.warning(f"Agent 诊断调用失败: {e}")
            return {
                "error_type": "unknown",
                "root_cause": str(e),
                "severity": "medium",
                "retry_recommended": True,
                "fix_strategy": "直接重试",
            }

    async def _generate_fix(
        self,
        diagnosis: dict,
        context: str,
    ) -> dict:
        """根据诊断生成具体修正动作"""
        error_type = diagnosis.get("error_type", "unknown")
        fix_strategy = diagnosis.get("fix_strategy", "")

        # 基于错误类型的规则引擎（不完全依赖LLM）
        fix: dict[str, Any] = {
            "fix_description": fix_strategy,
            "modified_kwargs": {},
        }

        if error_type == "超时":
            fix["fix_description"] = "增加超时时间并降低prompt长度"
            fix["modified_kwargs"]["timeout_multiplier"] = 2

        elif error_type == "解析":
            fix["fix_description"] = "调整prompt要求简化输出格式"
            fix["modified_kwargs"]["simplify_output"] = True

        elif error_type == "模型":
            fix["fix_description"] = "简化prompt，降低复杂度"
            fix["modified_kwargs"]["reduce_complexity"] = True

        elif error_type == "网络":
            fix["fix_description"] = "等待后重试"
            await asyncio.sleep(2)  # 退避等待

        # 如果规则引擎无法处理，使用 LLM 生成修正
        if not fix["modified_kwargs"] and diagnosis.get("retry_recommended", True):
            try:
                prompt = f"""根据诊断结果，生成具体修正建议:
诊断: {json.dumps(diagnosis, ensure_ascii=False)}
上下文: {context}

返回JSON: {{"fix_description": "修正描述", "wait_seconds": 0}}"""

                llm_fix = await self.llm.chat_json(
                    prompt=prompt,
                    system_prompt="返回纯JSON修正方案。",
                    temperature=0.1,
                )
                fix["fix_description"] = llm_fix.get("fix_description", fix_strategy)

                wait = llm_fix.get("wait_seconds", 0)
                if wait > 0:
                    logger.info(f"Agent 建议等待 {wait}s 后重试")
                    await asyncio.sleep(min(wait, 10))

            except Exception:
                pass

        logger.info(f"Agent 修正方案: {fix['fix_description']}")
        return fix

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, **kwargs) -> ToolResult:
        """调用注册的工具"""
        tool = self.tools.get(tool_name)
        if not tool:
            return ToolResult(
                status=ToolResultStatus.FAILURE,
                error=f"工具未注册: {tool_name}",
            )

        step = self._new_step(f"tool:{tool_name}", str(kwargs)[:100])
        t0 = time.time()

        try:
            result = await tool.func(**kwargs)
            elapsed = (time.time() - t0) * 1000
            result.elapsed_ms = elapsed
            step.result = result
            return result
        except Exception as e:
            elapsed = (time.time() - t0) * 1000
            result = ToolResult(
                status=ToolResultStatus.FAILURE,
                error=str(e),
                elapsed_ms=elapsed,
            )
            step.result = result
            return result

    # ------------------------------------------------------------------
    # 经验查询
    # ------------------------------------------------------------------

    def get_experience_summary(self) -> str:
        """获取经验摘要"""
        if not self.experience:
            return "暂无历史经验"

        success = sum(1 for e in self.experience if e.success)
        total = len(self.experience)
        lines = [f"总经验: {total} 条 (成功 {success}, 失败 {total - success})"]

        # 最近5条
        for e in self.experience[-5:]:
            status = "✓" if e.success else "✗"
            lines.append(f"  {status} [{e.context}] {e.fix_applied}")

        return "\n".join(lines)

    def get_execution_log(self) -> list[dict]:
        """获取执行历史"""
        return [
            {
                "step_id": s.step_id,
                "action": s.action,
                "input": s.input_summary,
                "state": s.state.value,
                "result_status": s.result.status.value if s.result else None,
                "result_error": s.result.error if s.result else None,
                "reflection": s.reflection,
                "elapsed_ms": s.result.elapsed_ms if s.result else None,
            }
            for s in self.steps
        ]

    def update_last_experience_success(self) -> None:
        """将最后一条经验标记为成功（重试成功后调用）"""
        if self.experience:
            self.experience[-1].success = True
