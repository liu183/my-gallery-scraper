"""Scraper for everiaclub.com.

Strategy: list latest posts from the homepage (paginated), pick the newest
URL not yet in state['processed'], download all its images, write artifact.
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scrapers.common import (
    build_client,
    download_gallery,
    emit_github_output,
    fetch_html,
    load_state,
    mark_processed,
    save_state,
    slugify,
    write_artifact_meta,
)

SITE = "everiaclub"
BASE = "https://www.everiaclub.com/"
STATE_FILE = Path("state/everiaclub.json")
OUT_DIR = Path("output")
PAGES_TO_SCAN = 5  # scan up to 5 listing pages for unprocessed posts


def _prime_cloudflare(client) -> None:
    """Hit the site root with browser-shaped headers to obtain cf_clearance
    cookie before fetching listing pages. Cross-origin Google warmup tends
    to *increase* CF suspicion, so we go direct."""
    # Add full Sec-* + Sec-Ch-Ua header set that Chrome 124 actually sends.
    client.session.headers.update({
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Accept-Encoding": "gzip, deflate, br, zstd",
    })
    for attempt in range(3):
        try:
            r = client.session.get(BASE, timeout=30, allow_redirects=True)
            if r.status_code < 400:
                return
        except Exception:  # noqa: BLE001
            pass
        # Brief pause; CF challenge may need a second hit
        import time
        time.sleep(2 + attempt * 2)


def list_post_urls(client) -> list[str]:
    """Return post URLs from newest to oldest across first N pages."""
    _prime_cloudflare(client)
    urls: list[str] = []
    # After priming, set headers that match in-site navigation.
    client.session.headers.update({
        "Sec-Fetch-Site": "same-origin",
        "Referer": BASE,
    })
    for page in range(1, PAGES_TO_SCAN + 1):
        url = BASE if page == 1 else f"{BASE}page/{page}/"
        try:
            html = fetch_html(client, url)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] list page {url}: {e}")
            break
        soup = BeautifulSoup(html, "lxml")
        # Common WordPress pattern: article > h2.entry-title > a
        found = []
        for a in soup.select("article h2 a, article .entry-title a, h2.entry-title a"):
            href = a.get("href")
            if href and href not in found:
                found.append(href)
        if not found:
            # fallback: any link looking like a post permalink
            for a in soup.select("a[rel='bookmark']"):
                href = a.get("href")
                if href:
                    found.append(href)
        for u in found:
            if u not in urls:
                urls.append(u)
    return urls


def extract_gallery(client, post_url: str) -> tuple[str, list[str]]:
    """Return (title, image_urls) from a single post."""
    html = fetch_html(client, post_url)
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one("h1.entry-title, h1, .entry-title")
    title = title_el.get_text(strip=True) if title_el else "Untitled"
    img_urls: list[str] = []
    seen = set()
    # Prefer images inside the article body
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
        full = urljoin(post_url, src)
        # Skip tiny icons / avatars / ads heuristically
        if any(skip in full.lower() for skip in ("avatar", "icon", "logo", "smile", "emoji")):
            continue
        if full in seen:
            continue
        seen.add(full)
        img_urls.append(full)
    return title, img_urls


def main() -> int:
    state = load_state(STATE_FILE)
    processed = set(state.get("processed", []))

    with build_client(referer=BASE) as client:
        post_urls = list_post_urls(client)
        target = next((u for u in post_urls if u not in processed), None)
        if not target:
            print("[info] no new posts to process")
            emit_github_output("status", "empty")
            return 0
        print(f"[info] target: {target}")
        title, img_urls = extract_gallery(client, target)
        print(f"[info] title={title!r} images={len(img_urls)}")
        if not img_urls:
            print("[warn] no images found; marking processed to avoid loop")
            mark_processed(state, target)
            save_state(STATE_FILE, state)
            emit_github_output("status", "empty")
            return 0
        slug = slugify(title)
        manifest = download_gallery(
            site=SITE,
            title=title,
            source_url=target,
            image_urls=img_urls,
            out_dir=OUT_DIR,
            referer=target,
        )
        write_artifact_meta(OUT_DIR, site=SITE, slug=slug)
        mark_processed(state, target)
        save_state(STATE_FILE, state)
        emit_github_output("status", "ok")
        emit_github_output("slug", slug)
        emit_github_output("image_count", str(manifest.image_count))
        print(f"[done] downloaded {manifest.image_count} images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
