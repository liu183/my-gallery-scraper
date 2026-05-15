"""Scraper for bestgirlsexy.com (japan + china categories, alternating).

State tracks per-category processed lists and which category was last used,
so the two categories alternate across hourly runs.
"""
from __future__ import annotations

import sys
from pathlib import Path
import re
from urllib.parse import urljoin

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

SITE = "bestgirlsexy"
BASE = "https://bestgirlsexy.com/"
CATEGORIES = {
    "japan": "https://bestgirlsexy.com/category/japan/",
    "china": "https://bestgirlsexy.com/category/china/",
}
STATE_FILE = Path("state/bestgirlsexy.json")
OUT_DIR = Path("output")
PAGES_TO_SCAN = 5


def list_posts(client, cat_url: str) -> list[str]:
    urls: list[str] = []
    base_host = "bestgirlsexy.com"
    for page in range(1, PAGES_TO_SCAN + 1):
        url = cat_url if page == 1 else f"{cat_url.rstrip('/')}/page/{page}/"
        try:
            html = fetch_html(client, url)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {url}: {e}")
            break
        soup = BeautifulSoup(html, "lxml")
        found: list[str] = []
        # Try a broad set of selectors used by various WordPress themes
        selectors = [
            "article h2 a", "article h3 a", "article .entry-title a",
            "h2.entry-title a", "h2.post-title a", ".post-title a",
            "a[rel='bookmark']", ".entry-header a", ".post-header a",
            "article a.permalink", ".grid-item a", ".post-item a",
            ".list-item a", ".thumbnail a",
        ]
        for sel in selectors:
            for a in soup.select(sel):
                href = a.get("href")
                if href and href not in found:
                    found.append(href)
            if found:
                break
        # Last-resort fallback: any internal article-looking link
        if not found:
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                if (
                    base_host in href
                    and href != cat_url
                    and "/category/" not in href
                    and "/page/" not in href
                    and "/tag/" not in href
                    and "/author/" not in href
                    and "#" not in href
                    and href.rstrip("/").count("/") >= 3  # has slug, not just root
                ):
                    if href not in found:
                        found.append(href)
        if not found:
            print(f"[debug] no post links found on {url}; dumping HTML")
            dump_debug_html(f"bestgirlsexy-{page}", html)
        for u in found:
            if u not in urls:
                urls.append(u)
    return urls


_SKIP_IMG_HINTS = (
    "avatar", "icon", "logo", "emoji", "smile", "loading", "spinner",
    "favicon", "/themes/", "/wp-content/themes/", "gravatar", "ad-banner",
    "/plugins/", "wp-includes",
)


def _extract_images(soup, post_url: str) -> list[str]:
    img_urls: list[str] = []
    seen = set()
    # Prefer content container, but also scan body as fallback
    containers = [
        soup.select_one("article .entry-content"),
        soup.select_one("article"),
        soup.select_one(".entry-content"),
        soup.select_one(".post-content"),
        soup.select_one("main"),
        soup,
    ]
    container = next((c for c in containers if c is not None), soup)
    candidates = container.select("img")
    if len(candidates) <= 1:
        # Article wrapper had only 1 image — fall back to whole body
        candidates = soup.select("body img") or soup.select("img")
    for img in candidates:
        src = (
            img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
            or img.get("data-srcset", "").split(",")[0].strip().split(" ")[0]
            or img.get("srcset", "").split(",")[0].strip().split(" ")[0]
            or img.get("src")
        )
        if not src or src.startswith("data:"):
            continue
        full = urljoin(post_url, src)
        low = full.lower()
        if any(skip in low for skip in _SKIP_IMG_HINTS):
            continue
        # Skip tiny dimension hints (e.g. -150x150, -50x50)
        if re.search(r"-\d{1,2}x\d{1,2}\.(jpg|png|webp)", low):
            continue
        if full in seen:
            continue
        seen.add(full)
        img_urls.append(full)
    return img_urls


def extract_gallery(client, post_url: str) -> tuple[str, list[str]]:
    html = fetch_html(client, post_url)
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one("h1.entry-title, h1, .entry-title, title")
    title = title_el.get_text(strip=True) if title_el else "Untitled"
    img_urls = _extract_images(soup, post_url)
    if len(img_urls) <= 1:
        dump_debug_html(f"bestgirlsexy-detail-{slugify(post_url)}", html)
    return title, img_urls


def main() -> int:
    state = load_state(STATE_FILE)
    state.setdefault("per_category", {k: {"processed": []} for k in CATEGORIES})
    last_cat = state.get("last_category")
    order = ["japan", "china"]
    # Alternate: if last was japan, do china next
    start = (order.index(last_cat) + 1) % len(order) if last_cat in order else 0
    rotation = order[start:] + order[:start]

    with build_client(referer=BASE) as client:
        target = None
        chosen_cat = None
        for cat in rotation:
            processed = set(state["per_category"][cat].get("processed", []))
            posts = list_posts(client, CATEGORIES[cat])
            cand = next((u for u in posts if u not in processed), None)
            if cand:
                target = cand
                chosen_cat = cat
                break
        if not target:
            print("[info] nothing new in either category")
            emit_github_output("status", "empty")
            return 0

        print(f"[info] category={chosen_cat} target={target}")
        title, img_urls = extract_gallery(client, target)
        if not img_urls:
            state["per_category"][chosen_cat]["processed"].append(target)
            state["last_category"] = chosen_cat
            save_state(STATE_FILE, state)
            emit_github_output("status", "empty")
            return 0

        full_title = f"[{chosen_cat}] {title}"
        slug = slugify(f"{chosen_cat}-{title}")
        manifest = download_gallery(
            site=SITE,
            title=full_title,
            source_url=target,
            image_urls=img_urls,
            out_dir=OUT_DIR,
            referer=target,
        )
        write_artifact_meta(OUT_DIR, site=SITE, slug=slug)

        cat_state = state["per_category"][chosen_cat]
        proc = cat_state.setdefault("processed", [])
        if target not in proc:
            proc.append(target)
        if len(proc) > 2000:
            del proc[: len(proc) - 2000]
        state["last_category"] = chosen_cat
        save_state(STATE_FILE, state)

        emit_github_output("status", "ok")
        emit_github_output("slug", slug)
        emit_github_output("image_count", str(manifest.image_count))
        print(f"[done] {chosen_cat} {manifest.image_count} images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
