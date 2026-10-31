"""Take screenshots of the imported workflows in the n8n editor (used for docs/images/).

Needs the optional, env-provisioned local owner (N8N_INSTANCE_OWNER_* in .env) and Playwright:
    pip install playwright && python -m playwright install chromium
    python scripts/screenshot_canvas.py

The login password is read from N8N_UI_LOGIN_PASSWORD in the git-ignored .env file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from simulator.simulate import setting  # noqa: E402

BASE = f"http://127.0.0.1:{setting('N8N_PORT', '5678')}"
OUT = ROOT / "docs" / "images"
WORKFLOWS = {
    "dataopsAlertRtr1": "n8n-canvas-router.png",
    "dataopsDigest001": "n8n-canvas-digest.png",
    "dataopsErrorWf01": "n8n-canvas-error-handler.png",
}


def main() -> int:
    email, password = setting("N8N_INSTANCE_OWNER_EMAIL"), setting("N8N_UI_LOGIN_PASSWORD")
    if not email or not password:
        print("Set N8N_INSTANCE_OWNER_EMAIL and N8N_UI_LOGIN_PASSWORD in .env (see README).")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1800, "height": 950}, device_scale_factor=1)
        page.goto(f"{BASE}/signin", wait_until="load", timeout=90_000)
        page.locator("input[type=email], input[name=emailOrLdapLoginId], input[name=email]").first.fill(email)
        page.locator("input[type=password]").first.fill(password)
        page.keyboard.press("Enter")
        page.wait_for_url(lambda url: "/signin" not in url, timeout=60_000)
        for workflow_id, filename in WORKFLOWS.items():
            page.goto(f"{BASE}/workflow/{workflow_id}", wait_until="load", timeout=90_000)
            page.wait_for_selector("[data-test-id='canvas-node']", timeout=60_000)
            # Never click or type on the canvas: the editor autosaves, and a stray key press on a
            # selected node would change the workflow. Only the popover close link and the
            # fit-view control are used.
            close = page.locator("[data-test-id='suggested-actions-close']")
            try:
                close.first.wait_for(state="visible", timeout=5_000)
                close.first.click()            # dismiss the "Production checklist" popover
            except Exception:  # noqa: BLE001 - the popover does not always appear
                pass
            page.wait_for_function("Array.from(document.images).every(img => img.complete)", timeout=30_000)
            fit = page.locator("[data-test-id='zoom-to-fit']")
            if fit.count():
                fit.first.click()
            page.wait_for_timeout(1500)
            target = OUT / filename
            page.screenshot(path=str(target))
            print(f"saved {target.relative_to(ROOT)}")
        # Executions list of the router: shows the runs the simulator produced, including the failed one.
        page.goto(f"{BASE}/workflow/dataopsAlertRtr1/executions", wait_until="load", timeout=90_000)
        page.get_by_text("Success").first.wait_for(timeout=60_000)
        page.wait_for_timeout(3000)
        target = OUT / "n8n-executions-router.png"
        page.screenshot(path=str(target))
        print(f"saved {target.relative_to(ROOT)}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
