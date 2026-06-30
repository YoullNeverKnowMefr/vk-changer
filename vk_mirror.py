"""Mirrors VK group wall posts onto a VK channel, preserving paragraph
structure and photos, and keeps running indefinitely (for a 24/7 Windows
service / scheduled task). Login is manual on first run; the session is
then reused from disk on every subsequent run.
"""

import argparse
import json
import logging
import random
import sys
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from selectors import GROUP, CHANNEL, LOGIN

BASE_DIR = Path(__file__).resolve().parent
AUTH_DIR = BASE_DIR / "auth"
DATA_DIR = BASE_DIR / "data"
TMP_DIR = BASE_DIR / "tmp"
LOG_DIR = BASE_DIR / "logs"
STATE_PATH = AUTH_DIR / "state.json"
PROCESSED_PATH = DATA_DIR / "processed.json"
CONFIG_PATH = BASE_DIR / "config.json"

for d in (AUTH_DIR, DATA_DIR, TMP_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("vk_mirror")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_file_handler = RotatingFileHandler(
    LOG_DIR / "vk_mirror.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_fmt)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_fmt)
logger.addHandler(_console_handler)


def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit(
            "Missing config.json. Copy config.example.json to config.json "
            "and fill in groupUrl/channelUrl."
        )
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_processed():
    if PROCESSED_PATH.exists():
        return set(json.loads(PROCESSED_PATH.read_text(encoding="utf-8")))
    return set()


def save_processed(processed):
    PROCESSED_PATH.write_text(json.dumps(sorted(processed), indent=2), encoding="utf-8")


def has_stored_session():
    return STATE_PATH.exists()


def is_logged_in(page) -> bool:
    try:
        page.wait_for_selector(LOGIN["logged_in_marker"], timeout=5000)
        return True
    except Exception:
        return False


def perform_manual_login(browser):
    logger.info("No saved session found. Opening a visible browser for manual login...")
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://vk.com/")

    logger.info("Log in to VK in the opened browser window. Waiting up to 5 minutes...")
    deadline = time.time() + 5 * 60
    logged_in = False
    while time.time() < deadline:
        if is_logged_in(page):
            logged_in = True
            break
        time.sleep(2)

    if not logged_in:
        context.close()
        raise RuntimeError("Timed out waiting for manual login.")

    context.storage_state(path=str(STATE_PATH))
    logger.info(f"Session saved to {STATE_PATH}")
    context.close()


def extract_paragraphs(card, text_selector: str) -> list[str]:
    """Read the rendered text exactly as the browser lays it out, then split
    into paragraphs on the line breaks VK actually rendered, so blank lines
    and paragraph spacing match the source post."""
    el = card.locator(text_selector).first
    if el.count() == 0:
        return []
    rendered = el.evaluate("node => node.innerText")
    # innerText reflects real visual line breaks (including blank lines for
    # <br><br> or stacked <div>s), so a plain split preserves spacing.
    lines = rendered.replace("\r\n", "\n").split("\n")
    return lines


def scan_group_posts(page, group_url: str, max_posts: int, processed: set):
    page.goto(group_url, wait_until="domcontentloaded")
    time.sleep(random.uniform(0.8, 1.4))

    found = []
    attempts = 0

    while len(found) < max_posts and attempts < 8:
        cards = page.locator(GROUP["post_card"]).all()

        for card in cards:
            post_id = card.get_attribute("data-post-id")
            if not post_id or post_id in processed:
                continue
            if any(p["id"] == post_id for p in found):
                continue

            paragraphs = extract_paragraphs(card, GROUP["post_text"])

            images = []
            for img in card.locator(GROUP["post_image"]).all():
                src = img.get_attribute("src")
                if src:
                    images.append(src)

            found.append({"id": post_id, "paragraphs": paragraphs, "images": images})

        if len(found) >= max_posts:
            break

        page.mouse.wheel(0, 900)
        time.sleep(random.uniform(0.6, 1.1))
        attempts += 1

    # Oldest-first so the channel ends up in the same chronological order.
    return list(reversed(found[:max_posts]))


def download_image(page, url: str, index: int) -> Path:
    response = page.request.get(url)
    body = response.body()
    ext = Path(urlparse(url).path).suffix or ".jpg"
    file_path = TMP_DIR / f"img_{int(time.time() * 1000)}_{index}{ext}"
    file_path.write_bytes(body)
    return file_path


def type_paragraphs(page, textarea, paragraphs: list[str]):
    """Recreate the post's paragraph breaks and blank-line spacing exactly,
    by pasting each line through the clipboard and pressing Enter between
    them (typing raw \\n into a contenteditable is unreliable in VK's editor,
    but paste + Enter reproduces the same visual structure)."""
    textarea.click()
    for i, line in enumerate(paragraphs):
        if line:
            page.evaluate(
                "text => navigator.clipboard.writeText(text)", line
            )
            page.keyboard.press("Control+V")
        if i < len(paragraphs) - 1:
            page.keyboard.press("Enter")
        time.sleep(random.uniform(0.05, 0.15))


def repost_to_channel(page, channel_url: str, post: dict):
    page.goto(channel_url, wait_until="domcontentloaded")
    time.sleep(random.uniform(0.6, 1.0))

    page.locator(CHANNEL["open_composer_button"]).first.click()
    time.sleep(random.uniform(0.3, 0.6))

    if post["paragraphs"]:
        textarea = page.locator(CHANNEL["composer_text_area"]).first
        type_paragraphs(page, textarea, post["paragraphs"])

    if post["images"]:
        downloaded = [
            download_image(page, url, i) for i, url in enumerate(post["images"])
        ]

        page.locator(CHANNEL["attach_photo_button"]).first.click()
        time.sleep(random.uniform(0.4, 0.7))

        file_input = page.locator(CHANNEL["file_input"]).first
        file_input.set_input_files([str(p) for p in downloaded])
        time.sleep(random.uniform(1.5, 2.5))  # upload + VK processing time

        for p in downloaded:
            p.unlink(missing_ok=True)

    time.sleep(random.uniform(0.4, 0.7))
    page.locator(CHANNEL["submit_post_button"]).first.click()
    try:
        page.wait_for_selector(CHANNEL["post_success_toast"], timeout=15000)
    except Exception:
        # Not all VK layouts show a toast; absence isn't necessarily a failure.
        pass


def run_once(browser, config):
    processed = load_processed()
    context = browser.new_context(storage_state=str(STATE_PATH))
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    page = context.new_page()

    try:
        if not is_logged_in(page):
            raise RuntimeError(
                "Saved session is no longer valid. Run with --login-only to re-authenticate."
            )

        posts = scan_group_posts(
            page, config["groupUrl"], config.get("maxPostsPerScan", 10), processed
        )
        logger.info(f"Found {len(posts)} new group post(s) to mirror.")

        for post in posts:
            logger.info(f"Mirroring post {post['id']}...")
            try:
                repost_to_channel(page, config["channelUrl"], post)
                processed.add(post["id"])
                save_processed(processed)
                logger.info(f"  -> done ({post['id']})")
            except Exception:
                logger.error(f"  -> failed ({post['id']}): {traceback.format_exc()}")
            time.sleep(random.uniform(2.0, 5.0))  # space out posts
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--login-only", action="store_true")
    parser.add_argument("--once", action="store_true", help="Run a single scan/repost pass and exit instead of looping forever.")
    args = parser.parse_args()

    needs_login = args.login_only or not has_stored_session()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not needs_login)

        try:
            if needs_login:
                perform_manual_login(browser)
                if args.login_only:
                    return
                # Switch to a headless browser for the unattended run that follows.
                browser.close()
                browser = p.chromium.launch(headless=True)

            config = load_config()

            if args.once:
                run_once(browser, config)
                return

            interval = config.get("pollIntervalSeconds", 300)
            logger.info(f"Starting continuous loop, polling every {interval}s.")
            backoff = interval
            while True:
                try:
                    run_once(browser, config)
                    backoff = interval
                except Exception:
                    logger.error(f"Pass failed: {traceback.format_exc()}")
                    backoff = min(backoff * 2, 3600)
                time.sleep(backoff)
        finally:
            browser.close()


if __name__ == "__main__":
    while True:
        try:
            main()
            break
        except SystemExit:
            raise
        except Exception:
            logger.error(f"Fatal error, restarting browser/session in 30s: {traceback.format_exc()}")
            time.sleep(30)
