import asyncio
import json

from playwright.async_api import async_playwright

from pravda.constants import BROWSER_CHANNEL, BROWSER_HEADLESS, BROWSER_WS_URL


async def main():
    launch_options = json.dumps(
        {"channel": BROWSER_CHANNEL, "headless": BROWSER_HEADLESS}
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.connect(
            BROWSER_WS_URL,
            headers={"x-playwright-launch-options": launch_options},
        )
        page = await browser.new_page()
        await page.goto("https://example.com")
        await page.screenshot(path="screenshot.png")
        print(f"Title: {await page.title()}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
