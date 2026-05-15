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


def list_galleries(client, actress_url: str) -> list[tuple[str, str]]:
    """Find gallery URLs from an actress page.

    Gallery URLs typically live OUTSIDE /zyoyu/ on this site (under /post/,
    /gravure/, or year-month archives). They are NOT navigation links to
    other actresses.
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
        if "/zyoyu/" in full:  # navigation to other actresses, skip
            continue
        if any(p in full for p in ("/wp-", "/feed", "#", "/category/", "/tag/", "/author/")):
            continue
        if not (full.endswith(".html") or full.rstrip("/").count("/") >= 3):
            continue
        if full in seen:
            continue
        seen.add(full)
        results.append((text or "gallery", full))
    if not results:
        dump_debug_html(f"eropuru-actress-{slugify(actress_url)}", html)
    return results


def extract_gallery(client, gallery_url: str) -> tuple[str, list[str]]:
    html = fetch_html(client, gallery_url)
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one("h1, h2.entry-title, .entry-title, title")
    title = title_el.get_text(strip=True) if title_el else "Untitled"
    img_urls: list[str] = []
    seen = set()
    container = soup.select_one("article, .entry-content, .post-content, main") or soup
    for img in container.select("img"):
        src = (
            img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
            or img.get("src")
        )
        if not src:
            continue
        full = urljoin(gallery_url, src)
        if any(skip in full.lower() for skip in ("avatar", "icon", "logo", "emoji", "banner")):
            continue
        if full in seen:
            continue
        seen.add(full)
        img_urls.append(full)
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
