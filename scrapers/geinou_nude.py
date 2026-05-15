"""Scraper for geinou-nude.com. One gallery per task run."""
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

SITE = "geinou_nude"
BASE = "https://geinou-nude.com/"
STATE_FILE = Path("state/geinou_nude.json")
OUT_DIR = Path("output")
PAGES_TO_SCAN = 5


def list_post_urls(client) -> list[str]:
    urls: list[str] = []
    for page in range(1, PAGES_TO_SCAN + 1):
        url = BASE if page == 1 else f"{BASE}page/{page}/"
        try:
            html = fetch_html(client, url)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {url}: {e}")
            break
        soup = BeautifulSoup(html, "lxml")
        for a in soup.select(
            "article h2 a, article .entry-title a, h2.entry-title a, a[rel='bookmark']"
        ):
            href = a.get("href")
            if href and href not in urls:
                urls.append(href)
    return urls


def extract_gallery(client, post_url: str) -> tuple[str, list[str]]:
    html = fetch_html(client, post_url)
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

    with build_client(referer=BASE) as client:
        posts = list_post_urls(client)
        target = next((u for u in posts if u not in processed), None)
        if not target:
            print("[info] no new posts")
            emit_github_output("status", "empty")
            return 0
        print(f"[info] target={target}")
        title, img_urls = extract_gallery(client, target)
        if not img_urls:
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
        print(f"[done] {manifest.image_count} images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
