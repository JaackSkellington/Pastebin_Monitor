import asyncio
import aiohttp
import logging
import random
import re
import argparse
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────
#  Global Config
# ─────────────────────────────────────────────

BASE_URL = "https://pastebin.com"
ARCHIVE_URL = f"{BASE_URL}/archive"
RAW_URL = f"{BASE_URL}/raw"

DEFAULT_KEYWORDS_FILE = "keywords.txt"
DEFAULT_SEEN_FILE = "checked.txt"
RESULTS_DIR = Path("results")


DEFAULT_ARCHIVE_INTERVAL: int = 120

JITTER_MIN: float = 4.0
JITTER_MAX: float = 12.0

BACKOFF_403_429: int = 300  # 5 min

REQUEST_TIMEOUT: int = 20

USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]

# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("cti.pastebin")

# ─────────────────────────────────────────────
#  Regex for ID extract
# ─────────────────────────────────────────────

RE_ARCHIVE_ENTRY = re.compile(
    r'<a\s+href="/([A-Za-z0-9]+)\?source=[^"]*">([^<]+)</a>'
)


# ══════════════════════════════════════════════
#  State Management (Deduplication)
# ══════════════════════════════════════════════

class SeenStore:

    def __init__(self, seen_file: str = DEFAULT_SEEN_FILE) -> None:
        self._path = Path(seen_file)
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            ids = self._path.read_text(encoding="utf-8").splitlines()
            self._seen = set(filter(None, ids))
            log.info("State loaded: %d IDs already seen.", len(self._seen))
        else:
            log.info("File '%s' not found — starting empty state.", self._path)

    def contains(self, paste_id: str) -> bool:
        return paste_id in self._seen

    def add(self, paste_id: str) -> None:
        self._seen.add(paste_id)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(paste_id + "\n")

    def __len__(self) -> int:
        return len(self._seen)


# ══════════════════════════════════════════════
#  Keywords
# ══════════════════════════════════════════════

def load_keywords(keywords_file: str = DEFAULT_KEYWORDS_FILE) -> list[str]:
    """Reads the keywords file and returns a list with terms in lowercase."""
    path = Path(keywords_file)
    if not path.exists():
        log.error("Keywords file not found: %s", path)
        sys.exit(1)

    keywords = [
        line.strip().lower()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    if not keywords:
        log.error("No keyword found in '%s'.", path)
        sys.exit(1)

    log.info("Loaded keywords (%d): %s", len(keywords), keywords)
    return keywords


# ══════════════════════════════════════════════
#  HTTP Client Helper
# ══════════════════════════════════════════════

def _random_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "DNT": "1",
    }


async def _safe_get(
    session: aiohttp.ClientSession,
    url: str,
    *,
    is_raw: bool = False,
) -> Optional[str]:

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    headers = _random_headers()
    if is_raw:
        headers["Accept"] = "text/plain, */*"

    try:
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                return await resp.text(encoding="utf-8", errors="replace")

            if resp.status == 404:
                log.debug("404 — paste deleted: %s", url)
                return None

            if resp.status in (403, 429):
                log.warning(
                    "⚠️  %d received at %s — waiting %ds (backoff).",
                    resp.status, url, BACKOFF_403_429,
                )
                await asyncio.sleep(BACKOFF_403_429)
                return None

            log.warning("HTTP %d at %s", resp.status, url)
            return None

    except asyncio.TimeoutError:
        log.warning("Timeout accessing %s", url)
        return None
    except aiohttp.ClientError as exc:
        log.warning("Network error at %s: %s", url, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.error("Unexpected error at %s: %s", url, exc)
        return None


# ══════════════════════════════════════════════
#  Step 1 — Metadata Collection from /archive
# ══════════════════════════════════════════════

async def fetch_archive(session: aiohttp.ClientSession) -> list[dict[str, str]]:
    html = await _safe_get(session, ARCHIVE_URL)
    if not html:
        log.warning("Failed to get /archive.")
        return []

    matches = RE_ARCHIVE_ENTRY.findall(html)

    if not matches:
        log.warning(
            "No paste extracted from /archive — the HTML pattern might have changed.\n"
            "Check RE_ARCHIVE_ENTRY.\nHTML snippet: %.200s",
            html[:200],
        )
        return []

    seen_ids: set[str] = set()
    pastes: list[dict[str, str]] = []
    for paste_id, title in matches:
        if paste_id not in seen_ids:
            seen_ids.add(paste_id)
            pastes.append({"id": paste_id, "title": title.strip() or "(no title)"})

    log.info("📋  /archive — %d pastes found.", len(pastes))
    return pastes


# ══════════════════════════════════════════════
#  Step 2 — Filter by Title
# ══════════════════════════════════════════════

def match_title(title: str, keywords: list[str]) -> Optional[str]:

    title_lower = title.lower()
    for kw in keywords:
        if kw in title_lower:
            return kw
    return None


# ══════════════════════════════════════════════
#  Step 3 — Raw Data Collection
# ══════════════════════════════════════════════

async def fetch_raw(session: aiohttp.ClientSession, paste_id: str) -> Optional[str]:
    """Downloads the raw content of a paste."""
    url = f"{RAW_URL}/{paste_id}"
    return await _safe_get(session, url, is_raw=True)


# ══════════════════════════════════════════════
#  Step 4 — Content Filter
# ══════════════════════════════════════════════

def match_content(content: str, keywords: list[str]) -> list[str]:

    content_lower = content.lower()
    return [kw for kw in keywords if kw in content_lower]


# ══════════════════════════════════════════════
#  Saving Results
# ══════════════════════════════════════════════

def save_result(
    paste_id: str,
    title: str,
    content: str,
    matched_keywords: list[str],
    match_source: str,  # "title" | "content" | "both"
) -> None:

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    paste_url = f"{BASE_URL}/{paste_id}"
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    kw_prefix = "_".join(re.sub(r'[^\w\-]', '_', kw) for kw in matched_keywords)
    filename = RESULTS_DIR / f"{kw_prefix}_{paste_id}.txt"

    if filename.exists():
        log.debug("File already exists, skipping: %s", filename)
        return

    header = (
        f"# ─────────────────────────────────────────────\n"
        f"# CTI Pastebin Monitor — Match Detected\n"
        f"# ─────────────────────────────────────────────\n"
        f"# URL        : {paste_url}\n"
        f"# ID         : {paste_id}\n"
        f"# Title      : {title}\n"
        f"# Keywords   : {', '.join(matched_keywords)}\n"
        f"# Match in   : {match_source}\n"
        f"# Captured   : {timestamp}\n"
        f"# ─────────────────────────────────────────────\n\n"
    )

    filename.write_text(header + content, encoding="utf-8")
    log.info("💾  Saved: %s", filename)


# ══════════════════════════════════════════════
#  Processing a Single Paste
# ══════════════════════════════════════════════

async def process_paste(
    session: aiohttp.ClientSession,
    paste: dict[str, str],
    keywords: list[str],
    seen: SeenStore,
) -> None:

    paste_id: str = paste["id"]
    title: str = paste["title"]

    kw_from_title = match_title(title, keywords)
    title_match = kw_from_title is not None

    if title_match:
        log.info("🔍  [TITLE MATCH] ID=%s | kw='%s' | title='%s'",
                 paste_id, kw_from_title, title)

    jitter = random.uniform(JITTER_MIN, JITTER_MAX)
    log.debug("⏳  Waiting %.1fs (jitter) before downloading raw/%s", jitter, paste_id)
    await asyncio.sleep(jitter)

    content = await fetch_raw(session, paste_id)

    seen.add(paste_id)

    if content is None:
        if title_match:
            log.warning("ID=%s — match in title but failed to download content.", paste_id)
        return


    kws_in_content = match_content(content, keywords)
    content_match = len(kws_in_content) > 0

    if not title_match and not content_match:
        log.debug("ID=%s — no match. Discarded.", paste_id)
        return

    raw_matched = ([kw_from_title] if kw_from_title else []) + kws_in_content
    all_matched: list[str] = list(dict.fromkeys(raw_matched))

    if title_match and content_match:
        source = "both"
    elif title_match:
        source = "title"
    else:
        source = "content"

    log.warning(
        "🚨  MATCH [%s] ID=%s | title='%s' | keywords=%s",
        source.upper(), paste_id, title, all_matched,
    )

    save_result(paste_id, title, content, all_matched, source)


# ══════════════════════════════════════════════
#  Scan Loop
# ══════════════════════════════════════════════

async def scan_loop(
    keywords: list[str],
    seen: SeenStore,
    archive_interval: int,
) -> None:

    connector = aiohttp.TCPConnector(limit=5, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        cycle = 0
        while True:
            cycle += 1
            log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            log.info("🔄  Cycle #%d started | seen IDs: %d", cycle, len(seen))

            pastes = await fetch_archive(session)
            new_pastes = [p for p in pastes if not seen.contains(p["id"])]

            log.info("🆕  New pastes to process: %d", len(new_pastes))

            if new_pastes:
                sem = asyncio.Semaphore(3)

                async def _bounded_process(paste: dict[str, str]) -> None:
                    async with sem:
                        try:
                            await process_paste(session, paste, keywords, seen)
                        except Exception as exc:  # noqa: BLE001
                            log.error("Error processing ID=%s: %s", paste.get("id"), exc)

                tasks = [_bounded_process(p) for p in new_pastes]
                await asyncio.gather(*tasks)

            log.info("✅  Cycle #%d completed. Next in %ds.", cycle, archive_interval)
            await asyncio.sleep(archive_interval)


# ══════════════════════════════════════════════
#  Graceful Shutdown
# ══════════════════════════════════════════════

def _handle_signal(sig: int, loop: asyncio.AbstractEventLoop) -> None:
    log.info("Signal %s received — shutting down gracefully...", signal.Signals(sig).name)
    for task in asyncio.all_tasks(loop):
        task.cancel()


# ══════════════════════════════════════════════
#  Entry Point
# ══════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CTI Pastebin Monitor — detects leaks by keywords",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--keywords", "-k",
        default=DEFAULT_KEYWORDS_FILE,
        help="Keywords file (one per line).",
    )
    parser.add_argument(
        "--seen", "-s",
        default=DEFAULT_SEEN_FILE,
        help="File for persistence of already seen IDs.",
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=DEFAULT_ARCHIVE_INTERVAL,
        help="Interval (seconds) between /archive scans.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    log.info("╔══════════════════════════════════════════════╗")
    log.info("║   CTI Pastebin Monitor — Starting            ║")
    log.info("╚══════════════════════════════════════════════╝")
    log.info("Keywords file : %s", args.keywords)
    log.info("Seen file     : %s", args.seen)
    log.info("Interval      : %ds", args.interval)
    log.info("Results dir   : %s/", RESULTS_DIR)
    log.info("Jitter range  : %.1f–%.1fs", JITTER_MIN, JITTER_MAX)
    log.info("Backoff 4xx   : %ds", BACKOFF_403_429)

    keywords = load_keywords(args.keywords)
    seen = SeenStore(args.seen)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig, loop)

    try:
        await scan_loop(keywords, seen, args.interval)
    except asyncio.CancelledError:
        log.info("Monitor stopped.")


if __name__ == "__main__":
    asyncio.run(main())
