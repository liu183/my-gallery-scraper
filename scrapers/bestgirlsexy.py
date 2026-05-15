"""Scraper for bestgirlsexy.com (japan + china categories, alternating).

State tracks per-category processed lists and which category was last used,
so the two categories alternate across hourly runs.
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
    for page in range(1, PAGES_TO_SCAN + 1):
        url = cat_url if page == 1 else f"{cat_url.rstrip('/')}/page/{page}/"
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
        if any(skip in full.lower() for skip in ("avatar", "icon", "logo", "emoji")):
            continue
        if full in seen:
            continue
        seen.add(full)
        img_urls.append(full)
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
