"""Shared utilities for all scrapers.

Provides:
- HTTP client with retries, UA rotation, rate limiting
- Image download with size/count caps
- Manifest writer
- State file load/save
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

MAX_IMAGES_PER_GALLERY = 500
DEFAULT_TIMEOUT = 30.0
MIN_DELAY = 1.5
MAX_DELAY = 3.5

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]


def random_ua() -> str:
    return random.choice(UA_POOL)


def polite_sleep(min_s: float = MIN_DELAY, max_s: float = MAX_DELAY) -> None:
    time.sleep(random.uniform(min_s, max_s))


def build_client(referer: str | None = None) -> httpx.Client:
    headers = {
        "User-Agent": random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8,zh-CN;q=0.7",
    }
    if referer:
        headers["Referer"] = referer
    return httpx.Client(
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        http2=True,
    )


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True,
)
def fetch_html(client: httpx.Client, url: str) -> str:
    polite_sleep(0.8, 1.8)
    r = client.get(url)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True,
)
def download_image(client: httpx.Client, url: str, dst: Path) -> int:
    polite_sleep(0.4, 1.0)
    with client.stream("GET", url) as r:
        r.raise_for_status()
        total = 0
        with open(dst, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
    return total


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify(text: str, maxlen: int = 60) -> str:
    text = (text or "").strip()
    text = _SAFE.sub("-", text).strip("-")
    return (text[:maxlen] or "gallery").lower()


def guess_ext(url: str, default: str = ".jpg") -> str:
    p = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if p.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return default


@dataclass
class GalleryManifest:
    site: str
    title: str
    source_url: str
    preview: str           # local filename of preview image (relative)
    image_count: int
    images: list[str] = field(default_factory=list)  # ordered list of local filenames
    truncated: bool = False
    scraped_at: str = ""

    def write(self, out_dir: Path) -> None:
        (out_dir / "manifest.json").write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"processed": [], "cursor": None}


def save_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_processed(state: dict, url: str, cap: int = 2000) -> None:
    processed = state.setdefault("processed", [])
    if url not in processed:
        processed.append(url)
    # Trim oldest to keep state small
    if len(processed) > cap:
        del processed[: len(processed) - cap]
    state["cursor"] = url


def download_gallery(
    *,
    site: str,
    title: str,
    source_url: str,
    image_urls: Iterable[str],
    out_dir: Path,
    referer: str | None = None,
) -> GalleryManifest:
    """Download a list of image URLs, write manifest, return it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    urls = list(image_urls)
    truncated = False
    if len(urls) > MAX_IMAGES_PER_GALLERY:
        urls = urls[:MAX_IMAGES_PER_GALLERY]
        truncated = True

    images: list[str] = []
    client = build_client(referer=referer or source_url)
    with client:
        for idx, url in enumerate(urls, start=1):
            ext = guess_ext(url)
            fname = f"{idx:04d}{ext}"
            dst = out_dir / fname
            try:
                download_image(client, url, dst)
                images.append(fname)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] failed to download {url}: {e}")

    if not images:
        raise RuntimeError(f"no images downloaded for {source_url}")

    preview = images[0]
    manifest = GalleryManifest(
        site=site,
        title=title,
        source_url=source_url,
        preview=preview,
        image_count=len(images),
        images=images,
        truncated=truncated,
        scraped_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    manifest.write(out_dir)
    return manifest


def write_artifact_meta(out_dir: Path, *, site: str, slug: str) -> None:
    """Write small meta file to help notify workflow identify artifact."""
    meta = {
        "site": site,
        "slug": slug,
        "timestamp": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "artifact_name": f"gallery-{site}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{slug}",
    }
    (out_dir / "artifact-meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def emit_github_output(name: str, value: str) -> None:
    """Write to $GITHUB_OUTPUT for downstream workflow steps."""
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        print(f"::set-output name={name}::{value}")
        return
    with open(gh_out, "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")
