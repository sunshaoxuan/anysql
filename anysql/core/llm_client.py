"""
AnySQL LLM 客户端

封装 Ollama REST API，提供异步 chat 和 embed 功能。
支持流式/非流式响应、重试机制和完整日志记录。
"""

from __future__ import annotations

import json
import time
from typing import Optional

import httpx

from anysql.logger import logger


class LLMClient:
    """Ollama API 异步客户端"""

    def __init__(
        self,
        base_url: str = "http://ccnode.briconbric.com:22545",
        chat_model: str = "qwen3:14b",
        embed_model: str = "qwen3-embedding:8b",
        timeout: int = 120,
        max_retries: int = 3,
        temperature: float = 0.3,
    ):
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model
        self.embed_model = embed_model
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        self._client: Optional[httpx.AsyncClient] = None

        logger.info(
            f"LLMClient 初始化: url={self.base_url}, "
            f"chat={self.chat_model}, embed={self.embed_model}"
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=10.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Chat (生成)
    # ------------------------------------------------------------------

    async def chat(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> str:
        """
        调用 Ollama /api/generate 进行文本生成。

        返回完整的生成文本。
        """
        target_model = model or self.chat_model
        temp = temperature if temperature is not None else self.temperature

        payload = {
            "model": target_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temp,
                "num_ctx": 8192,
            },
        }
        if system_prompt:
            payload["system"] = system_prompt

        url = f"{self.base_url}/api/generate"

        for attempt in range(1, self.max_retries + 1):
            t0 = time.time()
            try:
                logger.debug(
                    f"LLM chat 请求 (attempt {attempt}/{self.max_retries}): "
                    f"model={target_model}, prompt_len={len(prompt)}"
                )
                client = await self._get_client()
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                response_text = data.get("response", "")
                elapsed = time.time() - t0

                logger.info(
                    f"LLM chat 完成: {elapsed:.1f}s, "
                    f"response_len={len(response_text)}, "
                    f"eval_count={data.get('eval_count', '?')}"
                )
                return response_text

            except httpx.TimeoutException:
                elapsed = time.time() - t0
                logger.warning(
                    f"LLM chat 超时 (attempt {attempt}): {elapsed:.1f}s"
                )
                if attempt == self.max_retries:
                    raise
            except httpx.HTTPStatusError as e:
                logger.error(
                    f"LLM chat HTTP 错误 (attempt {attempt}): "
                    f"status={e.response.status_code}"
                )
                if attempt == self.max_retries:
                    raise
            except Exception as e:
                logger.error(
                    f"LLM chat 异常 (attempt {attempt}): {type(e).__name__}: {e}"
                )
                if attempt == self.max_retries:
                    raise

        return ""  # 不应到达

    # ------------------------------------------------------------------
    # Chat with JSON output
    # ------------------------------------------------------------------

    async def chat_json(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: Optional[float] = None,
    ) -> dict:
        """
        调用 LLM 并期望 JSON 格式的返回。
        自动提取并解析 JSON。
        """
        raw = await self.chat(prompt, system_prompt, temperature)
        return self._extract_json(raw)

    @staticmethod
    def _extract_json(text: str) -> dict:
        """从 LLM 响应中提取 JSON（支持 markdown 代码块包裹）"""
        # 尝试直接解析
        stripped = text.strip()

        # 移除 markdown 代码块
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            # 去掉首尾的 ``` 行
            start = 1
            end = len(lines)
            for i in range(len(lines) - 1, 0, -1):
                if lines[i].strip().startswith("```"):
                    end = i
                    break
            stripped = "\n".join(lines[start:end]).strip()

        # 移除 think 标签（qwen3 的思考过程）
        if "<think>" in stripped:
            think_end = stripped.rfind("</think>")
            if think_end != -1:
                stripped = stripped[think_end + len("</think>"):].strip()

        # 尝试找 JSON 对象
        brace_start = stripped.find("{")
        brace_end = stripped.rfind("}")
        if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
            json_str = stripped[brace_start:brace_end + 1]
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass

        # 最终尝试
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}, raw_len={len(text)}")
            return {"_raw": text, "_parse_error": str(e)}

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    async def embed(
        self,
        texts: list[str],
        model: Optional[str] = None,
    ) -> list[list[float]]:
        """
        调用 Ollama /api/embed 生成向量。

        支持批量输入。
        """
        target_model = model or self.embed_model
        url = f"{self.base_url}/api/embed"

        all_embeddings: list[list[float]] = []

        # 分批处理（每批最多 10 条，避免超时）
        batch_size = 10
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]

            payload = {
                "model": target_model,
                "input": batch,
            }

            for attempt in range(1, self.max_retries + 1):
                t0 = time.time()
                try:
                    logger.debug(
                        f"Embed 请求 (batch {i // batch_size + 1}, "
                        f"attempt {attempt}): {len(batch)} 条文本"
                    )
                    client = await self._get_client()
                    resp = await client.post(url, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    embeddings = data.get("embeddings", [])
                    elapsed = time.time() - t0

                    logger.debug(
                        f"Embed 完成: {elapsed:.1f}s, "
                        f"维度={len(embeddings[0]) if embeddings else '?'}"
                    )
                    all_embeddings.extend(embeddings)
                    break

                except Exception as e:
                    logger.warning(
                        f"Embed 异常 (attempt {attempt}): "
                        f"{type(e).__name__}: {e}"
                    )
                    if attempt == self.max_retries:
                        # 对失败的批次填充空向量
                        logger.error(f"Embed 批次 {i // batch_size + 1} 最终失败")
                        all_embeddings.extend([[] for _ in batch])

        return all_embeddings

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    async def health_check(self) -> dict:
        """检查 Ollama 服务状态"""
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{self.base_url}/api/tags",
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            logger.info(f"LLM 服务正常, 可用模型: {models}")
            return {
                "status": "ok",
                "models": models,
                "has_chat_model": self.chat_model in models
                    or any(self.chat_model in m for m in models),
                "has_embed_model": self.embed_model in models
                    or any(self.embed_model in m for m in models),
            }
        except Exception as e:
            logger.error(f"LLM 服务健康检查失败: {e}")
            return {"status": "error", "error": str(e)}
