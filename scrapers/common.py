"""Shared utilities for all scrapers.

Uses curl_cffi (browser TLS fingerprint impersonation) to bypass Cloudflare
and other anti-bot WAFs that block plain Python clients.
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

from curl_cffi import requests as cffi_requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

MAX_IMAGES_PER_GALLERY = 500
DEFAULT_TIMEOUT = 45.0
MIN_DELAY = 1.5
MAX_DELAY = 3.5

# curl_cffi browser fingerprints to rotate. "chrome124" is the latest stable.
IMPERSONATE_POOL = ["chrome124", "chrome120", "chrome116", "edge101", "safari17_0"]


def random_impersonate() -> str:
    return random.choice(IMPERSONATE_POOL)


def polite_sleep(min_s: float = MIN_DELAY, max_s: float = MAX_DELAY) -> None:
    time.sleep(random.uniform(min_s, max_s))


class Client:
    """Thin wrapper over curl_cffi Session with retry-aware get/stream."""

    def __init__(self, referer: str | None = None) -> None:
        self.impersonate = random_impersonate()
        self.session = cffi_requests.Session(impersonate=self.impersonate)
        self.session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8,zh-CN;q=0.7",
            }
        )
        if referer:
            self.session.headers["Referer"] = referer

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *a) -> None:
        self.close()


def build_client(referer: str | None = None) -> Client:
    return Client(referer=referer)


class FetchError(RuntimeError):
    pass


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((FetchError,)),
    reraise=True,
)
def fetch_html(client: Client, url: str) -> str:
    polite_sleep(0.8, 1.8)
    try:
        r = client.session.get(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"network error for {url}: {e}") from e
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code} for {url}")
    # curl_cffi auto-decodes; if encoding missing, fallback
    try:
        return r.text
    except Exception:  # noqa: BLE001
        return r.content.decode("utf-8", errors="ignore")


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((FetchError,)),
    reraise=True,
)
def download_image(client: Client, url: str, dst: Path) -> int:
    polite_sleep(0.3, 0.9)
    try:
        r = client.session.get(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True, stream=True)
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"network error for {url}: {e}") from e
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code} for {url}")
    total = 0
    with open(dst, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
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
    preview: str
    image_count: int
    images: list[str] = field(default_factory=list)
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
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        print(f"::set-output name={name}::{value}")
        return
    with open(gh_out, "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def dump_debug_html(name: str, html: str, out_dir: Path = Path("output")) -> None:
    """Save HTML for offline debugging when selectors don't match."""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"debug-{name}.html").write_text(html, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
