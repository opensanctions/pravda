import asyncio
import json
import os

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

BROWSER_CHANNEL = "chrome"
BROWSER_HEADLESS = False


async def main():
    launch_options = json.dumps(
        {"channel": BROWSER_CHANNEL, "headless": BROWSER_HEADLESS}
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.connect(
            os.environ["BROWSER_WS_URL"],
            headers={"x-playwright-launch-options": launch_options},
        )
        page = await browser.new_page()
        await page.goto("https://example.com")
        await page.screenshot(path="screenshot.png")
        print(f"Title: {await page.title()}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
