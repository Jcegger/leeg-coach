#!/usr/bin/env python3
"""
fetch_guide.py — bypass Cloudflare Turnstile to fetch Mobafire (and similar) guides.

Must be run via Windows Python to avoid WSL2↔Windows networking constraints:
  powershell.exe -Command "C:\\ProgramData\\Anaconda3\\python.exe C:\\path\\fetch_guide.py <url> <output>"

WSL wrapper (from leeg root):
  /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -Command \
    "C:\\ProgramData\\Anaconda3\\python.exe C:\\Temp\\fetch_guide.py '<url>' 'C:\\Temp\\out.html'"

Then read result from /mnt/c/Temp/out.html in WSL.

Requirements (Windows Python):
  pip install --user playwright
  python -m playwright install chromium
"""
import sys
import time


def fetch(url: str, output_path: str, timeout_s: int = 90) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

        print(f"Fetching {url} ...")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        for i in range(timeout_s):
            title = page.title()
            print(f"  [{i:2}s] {title!r}")
            if "just a moment" not in title.lower() and "cloudflare" not in title.lower():
                time.sleep(2)  # let page finish rendering
                break
            time.sleep(1)
        else:
            print("WARNING: challenge did not clear — saving anyway")

        content = page.content()
        browser.close()

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Saved {len(content):,} chars → {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    fetch(sys.argv[1], sys.argv[2])
