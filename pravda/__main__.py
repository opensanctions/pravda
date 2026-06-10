import asyncio
import json

from playwright.async_api import async_playwright

BROWSER_WS = "ws://localhost:3000"
LAUNCH_OPTIONS = {"channel": "chrome", "headless": False}


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect(
            BROWSER_WS,
            headers={"x-playwright-launch-options": json.dumps(LAUNCH_OPTIONS)},
        )
        page = await browser.new_page()
        await page.goto("https://example.com")
        await page.screenshot(path="screenshot.png")
        print(f"Title: {await page.title()}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
