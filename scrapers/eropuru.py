"""Scraper for eropuru.com.

Source index: https://www.eropuru.com/zyoyu/index.html
Lists "女優" (actresses). Each actress page contains multiple galleries.
Strategy: iterate actresses in order; for each, iterate her galleries in order.
Each task run downloads exactly one gallery (the next un-processed one).
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scrapers.common import (
    build_client,
    download_gallery,
    dump_debug_html,
    emit_github_output,
    fetch_html,
    load_state,
    save_state,
    slugify,
    write_artifact_meta,
)

SITE = "eropuru"
INDEX_URL = "https://www.eropuru.com/zyoyu/index.html"
BASE = "https://www.eropuru.com/"
STATE_FILE = Path("state/eropuru.json")
OUT_DIR = Path("output")


_NAV_TEXT_PATTERNS = (
    "あ行", "か行", "さ行", "た行", "な行", "は行", "ま行", "や行", "ら行", "わ行",
    "ホーム", "サイトマップ", "プロフィール", "次へ", "前へ", "トップ", "TOP",
    "ホーム", "home", "Home",
)


def list_actresses(client) -> list[tuple[str, str]]:
    """Return list of (name, url) of actress detail pages from the zyoyu index.

    The index page contains hiragana row navigation (あ行, か行, ...) plus
    actress entries. We need to follow ALL non-nav /zyoyu/ links to find
    actresses. Each actress link points to a page like /zyoyu/<name>.html
    or /zyoyu/<row>/<name>.html
    """
    html = fetch_html(client, INDEX_URL)
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("#main, .content, main, body") or soup
    results: list[tuple[str, str]] = []
    seen = set()
    for a in container.select("a[href]"):
        href = a.get("href")
        text = a.get_text(strip=True)
        if not href or not text:
            continue
        # Filter out navigation/section links
        if text in _NAV_TEXT_PATTERNS:
            continue
        if len(text) > 30:  # likely not a person name
            continue
        full = urljoin(INDEX_URL, href)
        if "/zyoyu/" not in full:
            continue
        if full == INDEX_URL or full.rstrip("/").endswith("/zyoyu") or full.endswith("/index.html"):
            continue
        # Skip alphabet/row index pages (short slugs like "agyo", or single letters)
        path = urlparse(full).path
        last = path.rstrip("/").split("/")[-1].replace(".html", "")
        if len(last) <= 3:  # too short to be a name slug
            continue
        if full in seen:
            continue
        seen.add(full)
        results.append((text, full))
    if not results:
        dump_debug_html("eropuru-index", html)
    return results


_EROPURU_INDEX_DIRS = (
    "/zyoyu/", "/janru/", "/genre/", "/tag/", "/category/",
    "/maker/", "/kantoku/", "/sirizu/", "/ymd/", "/okini/", "/label/",
)
# Whitelist: a real gallery on eropuru lives under /package/<id>
_GALLERY_PREFIX = "/package/"


def list_galleries(client, actress_url: str) -> list[tuple[str, str]]:
    """Find gallery URLs from an actress page.

    Gallery URLs are NOT navigation links to other index pages. Exclude
    /zyoyu/ (actress index), /janru/ (genre index), /tag/, /category/.
    Galleries on this site typically have URLs like:
      /<slug>.html  or  /<year>/<month>/<slug>/
    """
    html = fetch_html(client, actress_url)
    soup = BeautifulSoup(html, "lxml")
    results: list[tuple[str, str]] = []
    seen = set()
    for a in soup.select("a[href]"):
        href = a.get("href")
        text = a.get_text(strip=True)
        if not href:
            continue
        full = urljoin(actress_url, href)
        if BASE not in full or full == actress_url:
            continue
        # Whitelist: must be a real gallery page
        if _GALLERY_PREFIX not in full:
            continue
        # Skip index/navigation pages (defense-in-depth)
        if any(d in full for d in _EROPURU_INDEX_DIRS):
            continue
        if any(p in full for p in ("/wp-", "/feed", "#", "/author/", "?", "/page/")):
            continue
        path = urlparse(full).path
        # Reject ANY index page: /<slug>/index.html or bare /
        if path.endswith("/index.html") or path in ("/", "/index.html"):
            continue
        # Reject short-slug directory pages like /ymd/, /okini/ (≤5 chars between slashes with no file)
        segments = [s for s in path.split("/") if s]
        if len(segments) == 1 and len(segments[0]) <= 6 and "." not in segments[0]:
            continue
        # Must look like a content page
        if not (path.endswith(".html") or path.rstrip("/").count("/") >= 2):
            continue
        # Gallery pages on eropuru are typically /zyoyu/<numeric-id>.html — but we
        # already excluded /zyoyu/. Real galleries live at /<slug>/<numeric>.html
        # or /<numeric>.html. Require either a numeric component or depth ≥ 2.
        last = segments[-1].replace(".html", "") if segments else ""
        if not last:
            continue
        if full in seen:
            continue
        seen.add(full)
        results.append((text or "gallery", full))
    if not results:
        dump_debug_html(f"eropuru-actress-{slugify(actress_url)}", html)
    return results


_SKIP_IMG_HINTS = (
    "avatar", "icon", "logo", "emoji", "smile", "loading", "spinner",
    "favicon", "/themes/", "/wp-content/themes/", "gravatar", "banner",
    "/plugins/", "wp-includes",
)


def extract_gallery(client, gallery_url: str) -> tuple[str, list[str]]:
    import re
    html = fetch_html(client, gallery_url)
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one("h1, h2.entry-title, .entry-title, title")
    title = title_el.get_text(strip=True) if title_el else "Untitled"
    img_urls: list[str] = []
    seen = set()
    containers = [
        soup.select_one("article .entry-content"),
        soup.select_one("article"),
        soup.select_one(".entry-content"),
        soup.select_one(".post-content"),
        soup.select_one("main"),
        soup,
    ]
    container = next((c for c in containers if c is not None), soup)
    imgs = container.select("img")
    if len(imgs) <= 1:
        imgs = soup.select("body img") or soup.select("img")
    for img in imgs:
        src = (
            img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
            or img.get("srcset", "").split(",")[0].strip().split(" ")[0]
            or img.get("src")
        )
        if not src or src.startswith("data:"):
            continue
        full = urljoin(gallery_url, src)
        low = full.lower()
        if any(skip in low for skip in _SKIP_IMG_HINTS):
            continue
        if re.search(r"-\d{1,2}x\d{1,2}\.(jpg|png|webp)", low):
            continue
        if full in seen:
            continue
        seen.add(full)
        img_urls.append(full)
    if len(img_urls) <= 1:
        dump_debug_html(f"eropuru-detail-{slugify(gallery_url)}", html)
    return title, img_urls


def main() -> int:
    state = load_state(STATE_FILE)
    processed = set(state.get("processed", []))
    actress_cursor = state.get("actress_cursor")  # last actress URL we were on

    with build_client(referer=BASE) as client:
        actresses = list_actresses(client)
        if not actresses:
            print("[error] no actresses parsed from index")
            emit_github_output("status", "empty")
            return 0

        # Start from cursor (inclusive) and wrap around
        start_idx = 0
        if actress_cursor:
            for i, (_, url) in enumerate(actresses):
                if url == actress_cursor:
                    start_idx = i
                    break
        order = actresses[start_idx:] + actresses[:start_idx]

        target = None
        target_actress = None
        target_title = None
        for name, aurl in order:
            galleries = list_galleries(client, aurl)
            cand = next(((t, u) for t, u in galleries if u not in processed), None)
            if cand:
                target_title, target = cand
                target_actress = (name, aurl)
                break

        if not target:
            print("[info] no new galleries across all actresses")
            emit_github_output("status", "empty")
            return 0

        print(f"[info] actress={target_actress[0]} target={target}")
        title, img_urls = extract_gallery(client, target)
        title = title or target_title or "Untitled"
        if not img_urls:
            processed.add(target)
            state["processed"] = list(processed)
            state["actress_cursor"] = target_actress[1]
            save_state(STATE_FILE, state)
            emit_github_output("status", "empty")
            return 0

        full_title = f"[{target_actress[0]}] {title}"
        slug = slugify(f"{target_actress[0]}-{title}")
        manifest = download_gallery(
            site=SITE,
            title=full_title,
            source_url=target,
            image_urls=img_urls,
            out_dir=OUT_DIR,
            referer=target,
        )
        write_artifact_meta(OUT_DIR, site=SITE, slug=slug)

        proc_list = state.setdefault("processed", [])
        if target not in proc_list:
            proc_list.append(target)
        if len(proc_list) > 5000:
            del proc_list[: len(proc_list) - 5000]
        state["actress_cursor"] = target_actress[1]
        save_state(STATE_FILE, state)

        emit_github_output("status", "ok")
        emit_github_output("slug", slug)
        emit_github_output("image_count", str(manifest.image_count))
        print(f"[done] {manifest.image_count} images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
