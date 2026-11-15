from __future__ import annotations

import re

from ..collectors.http import FetchError, Fetcher
from ..config import Settings, settings


class ReportSummarizer:
    """使用现有 OpenAI 兼容配置，为已生成的客观报告写摘要。"""

    def __init__(self, config: Settings = settings, fetcher: Fetcher | None = None) -> None:
        self.config = config
        self.fetcher = fetcher or Fetcher(config)

    def summarize(self, report_content: str) -> str:
        if not self.config.llm_api_key:
            raise RuntimeError("未配置 LLM_API_KEY")
        max_chars = self.config.llm_max_chars
        llm_input = report_content
        if len(report_content) > max_chars:
            llm_input = report_content[:max_chars].rsplit("\n", 1)[0]
            llm_input += "\n（报告原文超出模型输入上限，以上仅为可发送部分。）"
        prompt = f"""
请根据下面的竞品监测报告写一段中文摘要，150 字以内。
只概括原文已经记录的事实，不添加评价、建议、预测或原文之外的数字。
优先概括价格变动、上游模型上下线、重要功能、营销活动和预警落地情况。
如果没有变动，直接说明本期无新增变动。只输出摘要正文，不要标题或代码块。

报告原文：
{llm_input}
""".strip()
        response = self.fetcher.post_json(
            f"{self.config.llm_base_url}/chat/completions",
            payload={
                "model": self.config.llm_model,
                "messages": [
                    {"role": "system", "content": "你是严谨的竞品监测报告编辑，只概括给定事实。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                # 推理模型会先消耗 reasoning tokens，400 可能导致正文为空。
                "max_tokens": 1600,
            },
            headers={"Authorization": f"Bearer {self.config.llm_api_key}"},
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise FetchError("模型总结响应缺少 choices[0].message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise FetchError("模型总结响应为空或格式错误")
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", content, flags=re.IGNORECASE).strip()
        return content
