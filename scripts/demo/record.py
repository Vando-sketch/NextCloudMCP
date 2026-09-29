"""Record the terminal player and screenshot the Nextcloud UI with Playwright.

    python3 -m http.server 8099 &        # from the repo root
    uv run --with playwright python scripts/demo/record.py /tmp/demo-out

Needs the demo Nextcloud from run_demo.py on localhost:8088 (login demo /
demo-pass-2026). Writes demo.webm plus PNG screenshots into the output dir.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

NC = "http://localhost:8088"


def login(page: Page) -> None:
    page.goto(f"{NC}/login")
    page.fill("input[name=user]", "demo")
    page.fill("input[name=password]", "demo-pass-2026")
    page.click("button[type=submit]")
    page.wait_for_url("**/apps/**", timeout=60000)


def dismiss_dialogs(page: Page) -> None:
    for label in ("Close", "Skip", "Got it", "Schließen"):
        for btn in page.get_by_role("button", name=label).all():
            if btn.is_visible():
                btn.click()
                page.wait_for_timeout(300)


def screenshots(browser, out: Path) -> None:
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
    page = ctx.new_page()
    login(page)
    page.goto(f"{NC}/apps/calendar/timeGridWeek/now")
    page.wait_for_timeout(4000)
    dismiss_dialogs(page)
    page.screenshot(path=str(out / "calendar.png"))
    page.goto(f"{NC}/apps/tasks/")
    page.wait_for_timeout(4000)
    dismiss_dialogs(page)
    page.screenshot(path=str(out / "tasks.png"))
    page.goto(f"{NC}/apps/notes/")
    page.wait_for_timeout(4000)
    dismiss_dialogs(page)
    page.screenshot(path=str(out / "notes.png"))
    ctx.close()


def record(browser, out: Path) -> None:
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 720},
        record_video_dir=str(out / "video"),
        record_video_size={"width": 1280, "height": 720},
    )
    page = ctx.new_page()
    page.goto("http://localhost:8099/scripts/demo/player.html")
    page.wait_for_function("document.title === 'done'", timeout=180000)
    ctx.close()
    src = next((out / "video").glob("*.webm"))
    src.rename(out / "demo.webm")


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        screenshots(browser, out)
        record(browser, out)
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo-out")
