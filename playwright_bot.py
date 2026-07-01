"""Работа с VK через Playwright: ручной вход (cookie-сессия в постоянном
профиле Chromium), чтение стены сообщества и публикация в канал-мессенджер.

Ключевой момент: авторизация VK держится на cookie (remixsid...), а НЕ на
localStorage-токене vk_token_user. Поэтому сессия хранится в постоянном
профиле браузера (user_data_dir) — cookie переживают перезапуск сами. Файл
session.vk дополнительно сохраняется как storage_state (cookie + localStorage).
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError
from playwright.sync_api import Page, Playwright, sync_playwright

VK_HOME_URL = "https://vk.com/feed"
VK_START_URL = "https://vk.com"
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
AUTH_COOKIE_NAMES = ("remixsid", "remixsid6", "remixstid", "remixnsid")
LOGGED_IN_PATH_HINTS = (
    "/feed",
    "/id",
    "/club",
    "/public",
    "/im",
    "/mail",
    "/friends",
    "/groups",
    "/settings",
)


@dataclass
class WallPost:
    post_id: str
    text: str
    photo_urls: list[str] = field(default_factory=list)
    community_url: str = ""
    posted_at: int | None = None


class VkPlaywrightBot:
    def __init__(
        self,
        *,
        storage_path: Path,
        headless: bool = False,
        slow_mo_ms: int = 50,
        fresh: bool = False,
    ) -> None:
        self.storage_path = storage_path
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.fresh = fresh
        self._session_confirmed = False
        self._session_saved = False
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> "VkPlaywrightBot":
        self.start(fresh=self.fresh)
        return self

    def __exit__(self, *_args: Any) -> None:
        self.stop()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Браузер не запущен")
        return self._page

    def start(self, *, fresh: bool = False) -> None:
        if self._context is not None:
            return

        self._playwright = sync_playwright().start()
        profile_dir = self.storage_path.parent / "playwright_profile"
        if fresh and profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)
        profile_dir.mkdir(parents=True, exist_ok=True)

        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self.headless,
            slow_mo=self.slow_mo_ms,
            locale="ru-RU",
            viewport={"width": 1366, "height": 900},
            user_agent=CHROME_USER_AGENT,
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._browser = None
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        try:
            self._context.grant_permissions(["clipboard-read", "clipboard-write"])
        except PlaywrightError:
            pass
        logging.info("Браузер Playwright запущен (профиль: %s)", profile_dir)

    def stop(self) -> None:
        if self._context is not None:
            if self._session_confirmed and not self._session_saved:
                self.save_session()
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self._page = None
        logging.info("Браузер Playwright остановлен")

    def save_session(self) -> None:
        if self._context is None or not self._session_confirmed or self._session_saved:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(self.storage_path))
        self._session_saved = True
        logging.info("Сессия браузера сохранена: %s", self.storage_path)

    def _has_auth_cookie(self) -> bool:
        if self._context is None:
            return False
        for cookie in self._context.cookies():
            if cookie.get("name") not in AUTH_COOKIE_NAMES:
                continue
            if not cookie.get("value"):
                continue
            domain = cookie.get("domain", "")
            if "vk.com" in domain or "vk.ru" in domain:
                return True
        return False

    def _ensure_page_open(self) -> Page:
        page = self.page
        if page.is_closed():
            raise RuntimeError(
                "Браузер закрыт. Не закрывайте окно Chromium до завершения входа"
            )
        return page

    def _is_auth_url(self, url: str) -> bool:
        lowered = url.lower()
        return "id.vk.com/auth" in lowered or (
            "vk.com" in lowered and "/login" in lowered
        )

    def _is_vk_site_url(self, url: str) -> bool:
        lowered = url.lower()
        return "vk.com" in lowered or "vk.ru" in lowered

    def _has_visible_login_prompt(self, page: Page) -> bool:
        login_link = page.locator(
            'a:has-text("Войти"), button:has-text("Войти"), '
            '[data-testid="enter-another-account"]'
        )
        return login_link.count() > 0 and login_link.first.is_visible()

    def _has_visible_profile(self, page: Page) -> bool:
        profile_selectors = [
            "#top_nav_link",
            '[data-testid="leftmenuitem"]',
            ".TopNavBtn--profile",
            '[data-testid="account-menu"]',
        ]
        for selector in profile_selectors:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                return True
        return False

    def _has_vk_user_id(self, page: Page) -> bool:
        try:
            return bool(
                page.evaluate(
                    """() => {
                        if (window.cur && window.cur.user_id) return true;
                        if (window.vk && window.vk.id) return true;
                        const node = document.querySelector('[data-user-id]');
                        return !!(node && node.getAttribute('data-user-id'));
                    }"""
                )
            )
        except PlaywrightError:
            return False

    def _is_vk_homepage(self, url: str) -> bool:
        return bool(re.match(r"https?://(www\.)?vk\.(com|ru)/?$", url, re.I))

    def _page_looks_logged_in(self, page: Page) -> bool:
        if page.is_closed():
            return False

        current_url = page.url.lower()
        if self._is_auth_url(current_url):
            return False
        if not self._is_vk_site_url(current_url):
            return False
        if self._has_vk_user_id(page):
            return True
        if self._has_auth_cookie() and not self._has_visible_login_prompt(page):
            return True
        if self._has_visible_login_prompt(page):
            return False
        if self._has_visible_profile(page):
            return True
        if any(hint in current_url for hint in LOGGED_IN_PATH_HINTS):
            return True
        if self._is_vk_homepage(page.url) and not self._has_visible_login_prompt(page):
            return True
        return False

    def _ask_save_session(self, page: Page) -> bool:
        print(f"\nАвтопроверка не уверена. Текущий URL: {page.url}")
        if self._is_auth_url(page.url):
            print("Откройте https://vk.com и войдите через кнопку «Войти» на сайте.")
            return False
        answer = input("Сохранить текущую сессию как успешный вход? (y/N): ").strip().lower()
        return answer in ("y", "yes", "д", "да")

    def _safe_goto_feed(self) -> None:
        page = self._ensure_page_open()
        if self._page_looks_logged_in(page):
            return

        try:
            page.goto(VK_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightError as error:
            message = str(error).lower()
            if "interrupted" in message or "target closed" in message:
                time.sleep(3)
            else:
                raise
        time.sleep(1)

    def is_logged_in(self, *, navigate: bool = True) -> bool:
        page = self._ensure_page_open()

        if self._page_looks_logged_in(page):
            return True

        if not navigate:
            return False

        self._safe_goto_feed()
        return self._page_looks_logged_in(page)

    def wait_for_manual_login(self, *, force: bool = False) -> None:
        if not force and self.is_logged_in():
            logging.info("Сессия VK активна")
            self._session_confirmed = True
            return

        if self.headless:
            raise RuntimeError(
                "Сессия VK не найдена. Сначала выполните вход вручную:\n"
                "  python vk_mirror.py --login-only\n"
                "(без --headless, в открытом браузере)"
            )

        page = self.page
        logging.info("Откройте VK и войдите вручную: %s", VK_START_URL)
        page.goto(VK_START_URL, wait_until="domcontentloaded", timeout=60_000)

        print(
            "\n=== Вход в VK ===\n"
            "1. В браузере открыт https://vk.com (не id.vk.com).\n"
            "2. Нажмите «Войти» на сайте VK, если нужно, и пройдите SMS.\n"
            "3. Убедитесь, что видите ленту или свой профиль.\n"
            "4. Не закрывайте окно браузера.\n"
            "5. Нажмите Enter в этой консоли.\n"
        )
        input("Нажмите Enter после входа в VK... ")

        page = self._ensure_page_open()
        if self.is_logged_in(navigate=False):
            logging.info("Ручной вход подтверждён")
            self._session_confirmed = True
            self.save_session()
            return

        if self._ask_save_session(page):
            logging.info("Сессия сохранена по подтверждению пользователя")
            self._session_confirmed = True
            self.save_session()
            return

        raise RuntimeError(
            "Вход не подтверждён. Откройте https://vk.com или https://vk.com/feed "
            "в окне Playwright и повторите --login-only"
        )

    def ensure_logged_in(self) -> None:
        if self.is_logged_in():
            logging.info("Сессия VK активна")
            self._session_confirmed = True
            return
        self.wait_for_manual_login()

    def _click_first(self, selectors: list[str]) -> bool:
        page = self.page
        for selector in selectors:
            button = page.locator(selector).first
            if button.count() == 0 or not button.is_visible():
                continue
            button.click()
            return True
        return False

    def _normalize_community_url(self, url: str) -> str:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = f"https://{url.lstrip('/')}"
        parsed = urlparse(url)
        if not parsed.netloc:
            raise ValueError(f"Некорректная ссылка на сообщество: {url}")
        return url

    def _expand_truncated_posts(self) -> None:
        page = self.page
        expand_patterns = [
            'button:has-text("Читать далее")',
            'button:has-text("Показать полностью")',
            'a:has-text("Читать далее")',
            'a:has-text("Показать полностью")',
            '[class*="ShowMore"]',
            '[class*="showMore"]',
        ]
        for pattern in expand_patterns:
            buttons = page.locator(pattern)
            for index in range(min(buttons.count(), 30)):
                button = buttons.nth(index)
                try:
                    if not button.is_visible():
                        continue
                    button.click(timeout=2_000)
                    time.sleep(0.4)
                except PlaywrightError:
                    continue

    def _open_community_wall(self, url: str) -> None:
        page = self.page
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(2)

        wall_tab_selectors = [
            'a[href*="w=wall"]',
            'a:has-text("Стена")',
            '[data-testid="group_tab_wall"]',
            '#wall_tabs a.wall_tab',
        ]
        for selector in wall_tab_selectors:
            tab = page.locator(selector).first
            if tab.count() == 0 or not tab.is_visible():
                continue
            try:
                tab.click(timeout=5_000)
                time.sleep(2)
                break
            except PlaywrightError:
                continue

        try:
            page.wait_for_selector(
                '[data-post-id], div[id^="post-"], a[href*="wall"]',
                timeout=15_000,
            )
        except PlaywrightError:
            logging.warning("Посты на стене не появились за 15 сек, продолжаю...")

        for _ in range(8):
            page.mouse.wheel(0, 2000)
            time.sleep(0.8)

    def collect_wall_posts(self, community_url: str, *, limit: int) -> list[WallPost]:
        url = self._normalize_community_url(community_url)
        logging.info("Открываю стену сообщества: %s", url)
        self._open_community_wall(url)
        self._expand_truncated_posts()

        raw_posts: list[dict[str, Any]] = self.page.evaluate(
            """(limit) => {
                const result = [];
                const seen = new Set();

                const textSelectors = [
                    '.wall_post_text',
                    '.vkitPost__text',
                    '[data-testid="post_text"]',
                    '.post_text',
                    '[class*="Post__text"]',
                    '[class*="wall_text"]',
                    '[class*="vkitText"]',
                ];

                const isInsideComments = (el) => {
                    if (!el) return false;
                    return Boolean(el.closest(
                        '.replies, .wall_replies, .wall_reply_list, .reply, .wall_reply, ' +
                        '.reply_wrap, .reply_box, [class*="Replies"], [class*="Comment"]'
                    ));
                };

                const pickPostId = (node) => {
                    if (!node) return '';
                    for (const link of node.querySelectorAll('a[href*="wall"]')) {
                        const match = (link.href || '').match(/wall(-?\\d+_\\d+)/i);
                        if (match) return match[1];
                    }
                    return node.getAttribute('data-post-id')
                        || (node.id || '').replace(/^post-/, '');
                };

                const excludeFromPost = (root) => {
                    const clone = root.cloneNode(true);
                    const junkSelectors = [
                        '.replies', '.wall_replies', '.wall_reply_list',
                        '.reply', '.wall_reply', '.reply_wrap', '.reply_box',
                        '[class*="Comment"]', '[class*="Reply"]', '[class*="Replies"]',
                        '[class*="PostHeader"]', '.PostHeader', 'header',
                        '[data-testid="post_header"]', '[class*="PostFooter"]',
                        '[class*="LikeButton"]', '[class*="Share"]',
                    ];
                    for (const sel of junkSelectors) {
                        clone.querySelectorAll(sel).forEach((el) => el.remove());
                    }
                    clone.querySelectorAll(
                        '[class*="Avatar"], [class*="avatar"], img[class*="Avatar"]'
                    ).forEach((el) => el.remove());
                    return clone;
                };

                const pickPostContentRoot = (node) => {
                    if (!node) return null;
                    return excludeFromPost(node);
                };

                const isPostPhoto = (img) => {
                    if (!img) return false;
                    if (img.closest(
                        '[class*="Avatar"], [class*="avatar"], [class*="PostHeader"], .PostHeader, ' +
                        '.reply, .wall_reply, [class*="Comment"], [class*="Reply"], [class*="Replies"], ' +
                        '[class*="OwnerPhoto"], [class*="RichAvatar"], [class*="PostAuthor"]'
                    )) {
                        return false;
                    }
                    const w = img.naturalWidth || img.width || 0;
                    const h = img.naturalHeight || img.height || 0;
                    if (w > 0 && h > 0 && (w < 80 || h < 80)) return false;
                    const src = (img.currentSrc || img.src || '').toLowerCase();
                    if (!src || src.startsWith('data:') || src.includes('emoji')) return false;
                    if (/ava\\d|camera_|\\/images\\/(u|o)\\//.test(src)) return false;
                    if (/[?&](cs|pp|sz|size|w|h)=(\\d{1,2}x\\d{1,2}|\\d{1,2})(&|$)/.test(src)) {
                        return false;
                    }
                    return true;
                };

                const pickText = (node) => {
                    const root = pickPostContentRoot(node);
                    if (!root) return '';
                    for (const sel of textSelectors) {
                        const el = root.querySelector(sel);
                        if (el && el.innerText.trim()) return el.innerText.trim();
                    }
                    const body = root.querySelector(
                        '[class*="Post__content"], .post_content, .wall_post_cont'
                    );
                    if (!body) return '';
                    const copy = body.cloneNode(true);
                    for (const junk of copy.querySelectorAll(
                        'button, [role="button"], time, img, video, [class*="ShowMore"], ' +
                        '[class*="MediaGrid"], .post_media_wrap, .page_post_thumb_wrap'
                    )) {
                        junk.remove();
                    }
                    return (copy.innerText || '').trim();
                };

                const pickPhotos = (node) => {
                    const root = pickPostContentRoot(node);
                    if (!root) return [];
                    const attachmentSelectors = [
                        '.post_media_wrap img',
                        '.page_post_thumb_wrap img',
                        '.MediaGrid img',
                        '.thumb_map img',
                        '.vkitPhotoAlbumPhoto__image',
                        '[class*="MediaGrid"] img',
                        '[class*="PhotoAlbumPhoto"] img',
                        '[class*="PrimaryAttachment"] img',
                    ];
                    const photoUrls = [];
                    const seenUrls = new Set();
                    for (const sel of attachmentSelectors) {
                        for (const img of root.querySelectorAll(sel)) {
                            if (!isPostPhoto(img)) continue;
                            const src = img.currentSrc || img.src || '';
                            if (!src || seenUrls.has(src)) continue;
                            seenUrls.add(src);
                            photoUrls.push(src);
                        }
                    }
                    return photoUrls;
                };

                const pickPostedAt = (node) => {
                    if (!node) return null;
                    const timeSelectors = [
                        '.PostHeader time[datetime]',
                        '.PostHeader time[data-date]',
                        '.PostHeader [data-date]',
                        '.wall_head .rel_date[data-date]',
                        '[data-testid="post_date"] time',
                        '[data-testid="post_date"] [data-date]',
                        '.post_info .rel_date[data-date]',
                        '.wall_post_cont .rel_date[data-date]',
                    ];
                    const parseRaw = (raw) => {
                        if (!raw) return null;
                        const num = parseInt(raw, 10);
                        if (!Number.isNaN(num) && num > 1_000_000_000 && num < 10_000_000_000) {
                            return num;
                        }
                        if (!Number.isNaN(num) && num >= 10_000_000_000) {
                            return Math.floor(num / 1000);
                        }
                        const ts = Math.floor(new Date(raw).getTime() / 1000);
                        if (!Number.isNaN(ts) && ts > 0) return ts;
                        return null;
                    };
                    for (const sel of timeSelectors) {
                        for (const el of node.querySelectorAll(sel)) {
                            if (isInsideComments(el)) continue;
                            const raw = el.getAttribute('datetime')
                                || el.getAttribute('data-date')
                                || el.getAttribute('data-time');
                            const ts = parseRaw(raw);
                            if (ts) return ts;
                        }
                    }
                    for (const el of node.querySelectorAll('time[datetime], .rel_date[data-date]')) {
                        if (isInsideComments(el)) continue;
                        const raw = el.getAttribute('datetime') || el.getAttribute('data-date');
                        const ts = parseRaw(raw);
                        if (ts) return ts;
                    }
                    return null;
                };

                const pushPost = (postId, node) => {
                    if (!postId || seen.has(postId) || result.length >= limit) return;
                    seen.add(postId);
                    result.push({
                        post_id: postId,
                        text: node ? pickText(node) : '',
                        photo_urls: pickPhotos(node),
                        posted_at: node ? pickPostedAt(node) : null,
                    });
                };

                for (const node of document.querySelectorAll('[data-post-id], div[id^="post-"]')) {
                    pushPost(pickPostId(node), node);
                }

                for (const link of document.querySelectorAll('a[href*="wall"]')) {
                    const match = (link.href || '').match(/wall(-?\\d+)_([\\d]+)/i);
                    if (!match) continue;
                    const postId = `${match[1]}_${match[2]}`;
                    const container = link.closest(
                        '[data-post-id], [id^="post-"], article, [class*="Post"]'
                    ) || link.parentElement?.parentElement?.parentElement;
                    if (!seen.has(postId)) pushPost(postId, container);
                }

                return result;
            }""",
            limit,
        )

        posts = [
            WallPost(
                post_id=str(item["post_id"]),
                text=str(item.get("text") or ""),
                photo_urls=[str(u) for u in item.get("photo_urls", []) if u],
                community_url=url,
                posted_at=int(item["posted_at"]) if item.get("posted_at") else None,
            )
            for item in raw_posts
        ]
        logging.info("Найдено постов на стене: %s", len(posts))
        for post in posts[:5]:
            preview = post.text.replace("\n", " ")[:80]
            logging.info(
                "  пост %s: %s%s",
                post.post_id,
                preview or "(текст не извлечён)",
                "..." if len(post.text) > 80 else "",
            )
        return posts

    def _download_photos(self, urls: list[str]) -> list[Path]:
        paths: list[Path] = []
        for index, url in enumerate(urls, start=1):
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            suffix = ".jpg"
            match = re.search(r"\.(jpe?g|png|webp)", urlparse(url).path, re.I)
            if match:
                suffix = f".{match.group(1).lower()}"
            temp = tempfile.NamedTemporaryFile(
                suffix=f"_{index}{suffix}",
                delete=False,
            )
            temp.write(response.content)
            temp.close()
            paths.append(Path(temp.name))
        return paths

    def _channel_peer_id(self, channel_url: str) -> str | None:
        match = re.search(r"-(\d+)", channel_url)
        return f"-{match.group(1)}" if match else None

    def _open_channel(self, channel_url: str) -> None:
        page = self.page
        logging.info("Открываю канал: %s", channel_url)
        page.goto(channel_url, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(3)

        peer_id = self._channel_peer_id(channel_url)
        if peer_id and not self._has_message_input():
            alt_url = f"https://vk.com/im?sel={peer_id}"
            logging.info("Пробую альтернативный URL канала: %s", alt_url)
            page.goto(alt_url, wait_until="domcontentloaded", timeout=60_000)
            time.sleep(3)

        self._click_first([
            'button:has-text("Написать")',
            '[data-testid="convo_composer_input"]',
            ".ConvoComposer",
            ".im-chat-input",
            ".Composer",
        ])
        time.sleep(1)

    def _has_message_input(self) -> bool:
        page = self.page
        selectors = [
            '[data-testid="mail_text_input"] [contenteditable="true"]',
            '[data-testid="convo_composer_input"]',
            ".ConvoComposer [contenteditable='true']",
            ".im-chat-input--text [contenteditable='true']",
            '[role="textbox"][contenteditable="true"]',
            "div[contenteditable='true']",
        ]
        for selector in selectors:
            field = page.locator(selector).first
            if field.count() > 0 and field.is_visible():
                return True
        return False

    def _focus_message_input(self) -> bool:
        page = self.page
        input_selectors = [
            '[data-testid="mail_text_input"] [contenteditable="true"]',
            '[data-testid="convo_composer_input"]',
            ".ConvoComposer [contenteditable='true']",
            ".composer_richtext [contenteditable='true']",
            ".im-chat-input--text [contenteditable='true']",
            ".im_editable[contenteditable='true']",
            '[role="textbox"][contenteditable="true"]',
            '[contenteditable="true"][data-placeholder]',
            '[contenteditable="true"][aria-label*="Сообщение" i]',
            '[contenteditable="true"][aria-label*="сообщение" i]',
        ]

        for selector in input_selectors:
            fields = page.locator(selector)
            for index in range(min(fields.count(), 5)):
                field = fields.nth(index)
                if not field.is_visible():
                    continue
                try:
                    field.scroll_into_view_if_needed(timeout=3_000)
                    field.click(timeout=5_000)
                    return True
                except PlaywrightError:
                    continue

        try:
            clicked = page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        const rect = el.getBoundingClientRect();
                        return rect.width > 40 && rect.height > 16 && rect.bottom > 0;
                    };
                    const fields = Array.from(
                        document.querySelectorAll('[contenteditable="true"], [role="textbox"]')
                    ).filter(isVisible);
                    const bottomField = fields.sort(
                        (a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom
                    )[0];
                    if (!bottomField) return false;
                    bottomField.focus();
                    bottomField.click();
                    return true;
                }"""
            )
            return bool(clicked)
        except PlaywrightError:
            return False

    def _type_message(self, text: str) -> None:
        """Вставляет текст в поле ввода канала, сохраняя переносы строк /
        абзацы. Enter в мессенджере отправляет сообщение, поэтому переносы
        вставляются через insert_text / fill, а не нажатием Enter."""
        page = self.page
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        if not text:
            return

        focused = page.locator(
            '[contenteditable="true"]:focus, [role="textbox"]:focus'
        ).first
        try:
            if focused.count() > 0:
                focused.fill(text, timeout=5_000)
                return
        except PlaywrightError:
            pass

        page.keyboard.insert_text(text)

    def _wait_attachments_ready(self) -> None:
        time.sleep(2)
        try:
            self.page.wait_for_selector(
                '[class*="attach"], [class*="Attachment"], [data-testid*="attach"]',
                timeout=8_000,
            )
        except PlaywrightError:
            time.sleep(1)

    def publish_to_channel(
        self,
        channel_url: str,
        text: str,
        photo_urls: list[str] | None = None,
        *,
        return_to_url: str | None = None,
    ) -> None:
        page = self.page
        photo_urls = photo_urls or []
        temp_files: list[Path] = []

        try:
            self._open_channel(channel_url)

            try:
                page.wait_for_selector(
                    '[contenteditable="true"], [role="textbox"], [data-testid="convo_composer_input"]',
                    timeout=15_000,
                )
            except PlaywrightError:
                logging.warning("Поле ввода не появилось за 15 сек, продолжаю поиск...")

            if not self._focus_message_input():
                raise RuntimeError(
                    "Не найдено поле ввода сообщения в канале. "
                    f"URL: {page.url}. Убедитесь, что аккаунт — админ канала."
                )

            logging.info("Вставляю текст (%s символов)", len(text))
            self._type_message(text)
            time.sleep(1)

            if photo_urls:
                logging.info("Прикрепляю фото: %s шт.", len(photo_urls))
                temp_files = self._download_photos(photo_urls)
                attach_selectors = [
                    '[data-testid="attach_photo"]',
                    'button[aria-label*="фото" i]',
                    'button[aria-label*="photo" i]',
                    'button[aria-label*="Фото" i]',
                    ".ComposerButton--attach",
                    '[data-testid="attach"]',
                ]
                attached = False
                for selector in attach_selectors:
                    button = page.locator(selector).first
                    if button.count() == 0 or not button.is_visible():
                        continue
                    try:
                        with page.expect_file_chooser(timeout=10_000) as chooser_info:
                            button.click()
                        chooser = chooser_info.value
                        chooser.set_files([str(path) for path in temp_files])
                        attached = True
                        self._wait_attachments_ready()
                        break
                    except PlaywrightError:
                        continue

                if not attached:
                    file_input = page.locator('input[type="file"]').first
                    if file_input.count() > 0:
                        file_input.set_input_files([str(path) for path in temp_files])
                        attached = True
                        self._wait_attachments_ready()

                if not attached:
                    raise RuntimeError("Не удалось прикрепить фото к сообщению")

            sent = self._click_first([
                '[data-testid="send_button"]',
                '[data-testid="convo_send_button"]',
                'button[aria-label*="Отправить" i]',
                'button[aria-label*="отправить" i]',
                'button:has-text("Отправить")',
                ".im-send-btn",
                ".ConvoComposer__sendButton",
            ])
            if not sent:
                raise RuntimeError("Не найдена кнопка «Отправить»")

            time.sleep(2)
            logging.info("Сообщение отправлено в канал")

            if return_to_url:
                logging.info("Возвращаюсь к парсингу: %s", return_to_url)
                self._open_community_wall(return_to_url)
        finally:
            for path in temp_files:
                path.unlink(missing_ok=True)
