"""Scraper for everiaclub.com (Cloudflare-protected — uses Playwright).

Strategy: list latest posts from the homepage (paginated), pick the newest
URL not yet in state['processed'], download all its images, write artifact.
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scrapers.common import (
    emit_github_output,
    load_state,
    mark_processed,
    save_state,
    slugify,
    write_artifact_meta,
)
from scrapers.pw_client import PWClient, download_gallery_pw

SITE = "everiaclub"
BASE = "https://www.everiaclub.com/"
STATE_FILE = Path("state/everiaclub.json")
OUT_DIR = Path("output")
PAGES_TO_SCAN = 3


def list_post_urls(client: PWClient) -> list[str]:
    urls: list[str] = []
    for page in range(1, PAGES_TO_SCAN + 1):
        url = BASE if page == 1 else f"{BASE}page/{page}/"
        try:
            html = client.fetch_html(url, wait_selector="article")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] list page {url}: {e}")
            break
        soup = BeautifulSoup(html, "lxml")
        found: list[str] = []
        for a in soup.select("article h2 a, article .entry-title a, h2.entry-title a"):
            href = a.get("href")
            if href and href not in found:
                found.append(href)
        if not found:
            for a in soup.select("a[rel='bookmark']"):
                href = a.get("href")
                if href:
                    found.append(href)
        for u in found:
            if u not in urls:
                urls.append(u)
    return urls


def extract_gallery(client: PWClient, post_url: str) -> tuple[str, list[str]]:
    html = client.fetch_html(post_url, wait_selector=".entry-content, article")
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one("h1.entry-title, h1, .entry-title")
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
        full = urljoin(post_url, src)
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

    with PWClient(base_url=BASE) as client:
        post_urls = list_post_urls(client)
        print(f"[info] discovered {len(post_urls)} posts")
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
        manifest = download_gallery_pw(
            client=client,
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
