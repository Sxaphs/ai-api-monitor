from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:8000"
PAGES = {
    "changes": "/changes",
    "review": "/review",
    "reports": "/reports",
    "report-detail": "/reports/3",
}
VIEWPORTS = {"desktop": {"width": 1440, "height": 900}, "mobile": {"width": 375, "height": 812}}


def main() -> None:
    output = Path(__file__).resolve().parents[1] / "design-demos" / "production"
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for name, path in PAGES.items():
            for viewport_name, viewport in VIEWPORTS.items():
                page = browser.new_page(viewport=viewport, device_scale_factor=1)
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                response = page.goto(BASE_URL + path, wait_until="networkidle")
                assert response and response.status == 200, (name, response.status if response else None)
                overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
                assert not overflow, f"{name} {viewport_name} 出现页面级横向溢出"
                page.screenshot(
                    path=str(output / f"{name}-{viewport_name}.png"),
                    full_page=name == "reports",
                )
                assert not errors, f"{name} {viewport_name} 控制台错误：{errors}"
                print(f"ok {name} {viewport_name} {viewport['width']}x{viewport['height']}")
                page.close()
        browser.close()


if __name__ == "__main__":
    main()
