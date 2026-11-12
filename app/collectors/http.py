from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import USER_AGENTS, Settings, settings


class FetchError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class FetchResult:
    text: str
    status_code: int
    url: str
    content_type: str


class Fetcher:
    """httpx / Playwright 共用的重试、超时和 UA 轮换入口。"""

    def __init__(
        self,
        config: Settings = settings,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.timeout = config.request_timeout
        self.attempts = config.retry_attempts
        self.backoff = config.retry_backoff
        self.sleep = sleep
        self.transport = transport

    def fetch(
        self,
        url: str,
        *,
        method: str = "http",
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            request_headers = {"User-Agent": USER_AGENTS[attempt % len(USER_AGENTS)]}
            if headers:
                request_headers.update(headers)
            try:
                if method == "playwright":
                    return self._fetch_playwright(url, request_headers["User-Agent"])
                if method != "http":
                    raise ValueError(f"不支持的采集方式: {method}")
                return self._fetch_http(url, request_headers)
            except Exception as exc:  # 每个站点由上层独立捕获，此处只负责重试
                last_error = exc
                if attempt + 1 < self.attempts:
                    self.sleep(self.backoff * (2**attempt))
        assert last_error is not None
        raise FetchError(
            f"{type(last_error).__name__}: {last_error}",
            status_code=getattr(last_error, "status_code", None),
        ) from last_error

    def _fetch_http(self, url: str, headers: Mapping[str, str]) -> FetchResult:
        with httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            response = client.get(url, headers=headers)
            if response.status_code >= 400:
                raise FetchError(f"HTTP {response.status_code}", status_code=response.status_code)
            return FetchResult(
                text=response.text,
                status_code=response.status_code,
                url=str(response.url),
                content_type=response.headers.get("content-type", ""),
            )

    def _fetch_playwright(self, url: str, user_agent: str) -> FetchResult:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=user_agent)
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000)
                )
                if response is not None and response.status >= 400:
                    raise FetchError(f"HTTP {response.status}", status_code=response.status)
                return FetchResult(
                    text=page.content(),
                    status_code=response.status if response is not None else 200,
                    url=page.url,
                    content_type="text/html",
                )
            finally:
                browser.close()

    def post_json(
        self,
        url: str,
        *,
        payload: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            request_headers = {"User-Agent": USER_AGENTS[attempt % len(USER_AGENTS)]}
            if headers:
                request_headers.update(headers)
            try:
                with httpx.Client(
                    timeout=self.timeout, follow_redirects=True, transport=self.transport
                ) as client:
                    response = client.post(url, json=dict(payload), headers=request_headers)
                    if response.status_code >= 400:
                        raise FetchError(
                            f"HTTP {response.status_code}", status_code=response.status_code
                        )
                    data = response.json()
                    if not isinstance(data, dict):
                        raise FetchError("LLM 接口返回值不是 JSON 对象")
                    return data
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.attempts:
                    self.sleep(self.backoff * (2**attempt))
        assert last_error is not None
        raise FetchError(
            f"LLM 请求失败: {type(last_error).__name__}: {last_error}",
            status_code=getattr(last_error, "status_code", None),
        ) from last_error

    def fetch_json(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        result = self.fetch(url, headers=headers)
        payload = result.text
        import json

        if not payload.strip():
            raise FetchError("接口返回空响应")
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FetchError(f"接口返回非有效 JSON: {exc.msg}") from exc
        if not isinstance(data, dict):
            raise FetchError("接口返回值不是 JSON 对象")
        return data
