"""
Capture dashboard screenshots for README.
Run with: python docs/take_screenshots.py
Requires: playwright (pip install playwright && playwright install chromium)
The Dash server must be running on localhost:8050.
"""
import time
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8050"
OUT = "docs/screenshots"


def shot(page, name: str, delay: float = 0.8):
    time.sleep(delay)
    page.screenshot(path=f"{OUT}/{name}", full_page=False)
    print(f"  saved {OUT}/{name}")


def pick_dropdown(page, dropdown_id: str, search_text: str):
    """
    Interact with a Dash 4 (Radix UI) dcc.Dropdown.
    Opens it, types to filter, then presses Enter to select the first match.
    """
    page.click(f"#{dropdown_id}")
    time.sleep(0.4)
    page.keyboard.type(search_text)
    time.sleep(0.6)
    # Press Enter to select the first highlighted option
    page.keyboard.press("Enter")
    time.sleep(0.4)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})

    # ── 1. Landing page ─────────────────────────────────────────────────────
    print("Landing page…")
    page.goto(BASE + "/", wait_until="networkidle")
    shot(page, "landing.png", delay=2.0)

    # ── 2. Batch — empty state ───────────────────────────────────────────────
    print("Batch (empty)…")
    page.goto(BASE + "/batch", wait_until="networkidle")
    shot(page, "batch_empty.png", delay=1.0)

    # ── 3. Batch — with results ──────────────────────────────────────────────
    print("Batch (running — ~15 s for 30 runs)…")
    page.fill("#batch-runs", "30")
    page.click("#batch-run-btn")
    page.wait_for_selector("#batch-summary-cards .card", timeout=120_000)
    shot(page, "batch_results.png", delay=1.5)

    # ── 4. Map — empty state ─────────────────────────────────────────────────
    print("Map (empty)…")
    page.goto(BASE + "/map", wait_until="networkidle")
    shot(page, "map_empty.png", delay=2.0)

    # ── 5. Map — mid animation ───────────────────────────────────────────────
    print("Map (running ride: Park St → Copley)…")
    pick_dropdown(page, "map-origin", "Park Street")
    time.sleep(0.5)
    pick_dropdown(page, "map-dest", "Copley")
    time.sleep(0.5)
    page.click("#map-run-btn")
    page.wait_for_selector("#map-current-stop", timeout=30_000)
    time.sleep(4)
    shot(page, "map_running.png", delay=0.5)

    # ── 6. Map — end-of-ride modal ───────────────────────────────────────────
    print("Waiting for end-of-ride modal…")
    try:
        page.wait_for_selector("#map-end-modal .modal-content", timeout=90_000)
        shot(page, "map_modal.png", delay=1.0)
        print("  modal captured.")
    except Exception as e:
        print(f"  modal not reached within timeout: {e}")

    browser.close()
    print("All done.")
