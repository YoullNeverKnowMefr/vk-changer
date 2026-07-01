"""Зеркалирует посты со стен сообществ VK в каналы-мессенджеры VK, сохраняя
абзацы/переносы и фото, удаляя подпись. Работает 24/7 (Windows-сервер /
планировщик задач). Вход — вручную один раз (--login-only), далее cookie-
сессия переиспользуется из постоянного профиля браузера.

Конфиг config.json:
{
  "pairs": [
    { "groupUrl": "https://vk.com/yourgroup",
      "channelUrl": "https://vk.com/im/channels/-230930322",
      "signature": "Мы теперь и в Max" }
  ],
  "pollIntervalSeconds": 300,
  "maxPostsPerScan": 20,
  "headless": false
}
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from playwright_bot import VkPlaywrightBot, WallPost

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
SESSION_PATH = BASE_DIR / "session.vk"
PROCESSED_PATH = DATA_DIR / "processed.json"
CONFIG_PATH = BASE_DIR / "config.json"

_shutdown = False


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(
                LOG_DIR / "vk_mirror.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            ),
        ],
        force=True,
    )


def request_shutdown(signum: int, _frame: Any) -> None:
    global _shutdown
    _shutdown = True
    logging.info("Получен сигнал %s, завершение после текущей итерации...", signum)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        example = BASE_DIR / "config.example.json"
        if example.exists():
            CONFIG_PATH.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            logging.error("Создан config.json из примера. Заполните pairs и запустите снова.")
        raise SystemExit("Нет config.json — заполните настройки (pairs) и запустите снова.")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def get_pairs(config: dict[str, Any]) -> list[dict[str, str]]:
    if "pairs" in config:
        return config["pairs"]
    return [{"groupUrl": config["groupUrl"], "channelUrl": config["channelUrl"]}]


def load_processed() -> dict[str, list[str]]:
    if PROCESSED_PATH.exists():
        return json.loads(PROCESSED_PATH.read_text(encoding="utf-8"))
    return {}


def save_processed(processed: dict[str, list[str]]) -> None:
    PROCESSED_PATH.parent.mkdir(parents=True, exist_ok=True)
    serializable = {url: sorted(set(ids)) for url, ids in processed.items()}
    PROCESSED_PATH.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _norm_for_match(s: str) -> str:
    """Нормализация для сравнения подписи: нижний регистр, выкидываем пробелы,
    эмодзи и прочие символы, оставляя буквы/цифры (любой язык) и символы URL.
    Так «Мы теперь и в Мах 🚀 https://max.ru/x» и «мы теперь и в мах🚀https://max.ru/x»
    сравниваются одинаково."""
    keep_url = set(":/._-?&=%#@~+")
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch in keep_url)


def _is_link_line(line: str) -> bool:
    """Похожа ли строка на отдельную строку-ссылку (её надо удалить вместе с
    подписью, даже если в конфиге указан только текст без ссылки)."""
    t = line.strip()
    if not t:
        return False
    if t.lower().startswith(("http://", "https://")):
        return True
    # одиночный токен вида domain.tld/path
    return " " not in t and "." in t and "/" in t


def remove_signature(text: str, signature: str) -> str:
    """Удаляет подпись — она ВСЕГДА в конце поста (в ней текст + ссылка на
    мессенджер, иногда разбита на две строки). Оригинальное форматирование
    остального текста сохраняется точь-в-точь: одиночные/двойные переносы,
    пустые строки и отступы не меняются.

    Сравнение устойчиво к пробелам и эмодзи (см. _norm_for_match). Если
    signature пустой — просто удаляется последняя непустая строка."""
    lines = text.replace("\r\n", "\n").split("\n")

    # Обрежем висящие пустые строки в конце (они не часть текста).
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return text

    sig_norm = _norm_for_match(signature or "")

    if not sig_norm:
        # Подпись в конфиге не задана — убираем последнюю непустую строку.
        removed = lines.pop()
        logging.info("Удалена подпись (последняя строка): %s", removed.strip()[:150])
    else:
        removed_lines: list[str] = []
        # Удаляем с конца строки, каждая из которых — часть подписи. Это
        # покрывает варианты: подпись в 1 строку, подпись в 2 строки, а также
        # когда в конфиге указан только общий текст, а ссылка у каждой группы
        # своя (тогда строку-ссылку снимаем, если строка над ней — подпись).
        while lines and len(removed_lines) < 3:
            candidate = lines[-1]
            if not candidate.strip():
                lines.pop()  # пустая строка внутри/перед подписью
                continue
            cand_norm = _norm_for_match(candidate)
            if cand_norm and (cand_norm in sig_norm or sig_norm in cand_norm):
                removed_lines.append(lines.pop())
                continue
            # Строка-ссылка (напр. другая ссылка без эмодзи) — удаляем её,
            # только если строка над ней совпадает с подписью из конфига.
            if _is_link_line(candidate):
                prev = None
                for j in range(len(lines) - 2, -1, -1):
                    if lines[j].strip():
                        prev = _norm_for_match(lines[j])
                        break
                if prev and (prev in sig_norm or sig_norm in prev):
                    removed_lines.append(lines.pop())
                    continue
            break
        if removed_lines:
            joined = " | ".join(r.strip() for r in reversed(removed_lines))
            logging.info("Удалена подпись (%d стр.): %s", len(removed_lines), joined[:200])
        else:
            logging.info(
                "Подпись «%s» не найдена в конце поста — текст оставлен без изменений.",
                signature,
            )

    # Снова убираем ставшие лишними пустые строки в конце, форматирование
    # выше подписи не трогаем.
    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines)


def pair_signature(config: dict[str, Any], pair: dict[str, str]) -> str:
    return str(pair.get("signature", config.get("signature", ""))).strip()


def process_pair(
    bot: VkPlaywrightBot,
    config: dict[str, Any],
    pair: dict[str, str],
    processed: dict[str, list[str]],
) -> int:
    group_url = pair["groupUrl"]
    channel_url = pair["channelUrl"]
    limit = int(config.get("maxPostsPerScan", 20))
    signature = pair_signature(config, pair)

    posts = bot.collect_wall_posts(group_url, limit=limit)

    # Первый запуск для этой группы: помечаем всё существующее как «уже видели»
    # и НЕ репостим — в канал попадут только посты, вышедшие после запуска.
    if group_url not in processed:
        processed[group_url] = [p.post_id for p in posts]
        save_processed(processed)
        logging.info(
            "[%s] Первый запуск: помечено %s существующих постов как старые (не репостим).",
            group_url,
            len(posts),
        )
        return 0

    seen = set(processed.get(group_url, []))
    new_posts = [p for p in posts if p.post_id not in seen]
    # Старые -> новые, чтобы в канале сохранился хронологический порядок.
    new_posts = list(reversed(new_posts))
    logging.info("[%s] Новых постов: %s -> %s", group_url, len(new_posts), channel_url)

    copied = 0
    for post in new_posts:
        text = remove_signature(post.text, signature)
        if not text.strip() and not post.photo_urls:
            logging.info("[%s] Пост %s пустой — пропуск.", group_url, post.post_id)
            seen.add(post.post_id)
            processed[group_url] = sorted(seen)
            save_processed(processed)
            continue

        try:
            bot.publish_to_channel(
                channel_url, text, post.photo_urls, return_to_url=group_url
            )
        except Exception:
            logging.exception("[%s] Ошибка публикации поста %s", group_url, post.post_id)
            continue

        seen.add(post.post_id)
        processed[group_url] = sorted(seen)
        save_processed(processed)
        copied += 1
        logging.info("[%s] Скопирован пост %s -> %s", group_url, post.post_id, channel_url)

    return copied


def run_once(bot: VkPlaywrightBot, config: dict[str, Any], processed: dict[str, list[str]]) -> int:
    total = 0
    for pair in get_pairs(config):
        try:
            total += process_pair(bot, config, pair, processed)
        except Exception:
            logging.exception("[%s] Пара завершилась с ошибкой", pair.get("groupUrl"))
    return total


def create_bot(config: dict[str, Any], *, headless: bool, fresh: bool = False) -> VkPlaywrightBot:
    return VkPlaywrightBot(
        storage_path=SESSION_PATH,
        headless=headless,
        slow_mo_ms=int(config.get("playwrightSlowMoMs", 50)),
        fresh=fresh,
    )


def sleep_interruptible(seconds: int) -> None:
    end_at = time.time() + seconds
    while time.time() < end_at and not _shutdown:
        time.sleep(min(1, max(0, end_at - time.time())))


def main() -> None:
    parser = argparse.ArgumentParser(description="Зеркалирование постов VK-сообществ в каналы")
    parser.add_argument("--once", action="store_true", help="Одна проверка и выход")
    parser.add_argument("--login-only", action="store_true", help="Ручной вход в VK и сохранение сессии")
    parser.add_argument("--headless", action="store_true", help="Браузер без окна")
    args = parser.parse_args()

    setup_logging()
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    # --login-only можно запускать без config.json.
    config = load_config() if (CONFIG_PATH.exists() or not args.login_only) else {}

    if args.login_only:
        with create_bot(config, headless=False, fresh=False) as bot:
            bot.wait_for_manual_login(force=True)
        logging.info("Вход выполнен, сессия сохранена. Теперь запускайте без --login-only.")
        return

    headless = bool(args.headless or config.get("headless", False))
    processed = load_processed()

    if args.once:
        with create_bot(config, headless=headless) as bot:
            bot.ensure_logged_in()
            copied = run_once(bot, config, processed)
        logging.info("Готово. Скопировано новых постов: %s", copied)
        return

    interval = int(config.get("pollIntervalSeconds", 300))
    logging.info("Режим мониторинга 24/7. Интервал: %s сек.", interval)
    with create_bot(config, headless=headless) as bot:
        bot.ensure_logged_in()
        while not _shutdown:
            try:
                copied = run_once(bot, config, processed)
                logging.info("Проверка завершена. Скопировано: %s", copied)
            except Exception:
                logging.exception("Ошибка во время проверки")
            if _shutdown:
                break
            logging.info("Следующая проверка через %s сек.", interval)
            sleep_interruptible(interval)
    logging.info("Сервис остановлен")


if __name__ == "__main__":
    main()
