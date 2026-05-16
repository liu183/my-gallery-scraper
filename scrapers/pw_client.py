"""Playwright-based fetcher for sites whose Cloudflare config rejects
curl_cffi (e.g. everiaclub.com).

We launch a real headless Chromium, navigate to URLs, wait for any CF
interstitial to clear, and return page HTML. Image downloads reuse the
authenticated browser context so cookies (cf_clearance) flow through.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page


class PWClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._pw = sync_playwright().start()
        self.browser: Browser = self._pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        self.context: BrowserContext = self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            viewport={"width": 1366, "height": 900},
        )
        # Suppress webdriver flag
        self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        self.page: Page = self.context.new_page()

    def close(self) -> None:
        try:
            self.context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._pw.stop()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "PWClient":
        return self

    def __exit__(self, *a) -> None:
        self.close()

    def fetch_html(self, url: str, wait_selector: str | None = None) -> str:
        """Navigate to URL, wait for content, return HTML.

        Handles Cloudflare interstitial by waiting up to 20s for any
        challenge to clear (cf_clearance cookie issued, page reloaded).
        """
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # CF challenge may show a "Verifying you are human" page; wait
                # for the real content selector or a generic <article>/<main>.
                target = wait_selector or "article, main, .entry-content, .post"
                try:
                    self.page.wait_for_selector(target, timeout=20000)
                except Exception:  # noqa: BLE001
                    # Even if no selector matched, the HTML may still be useful
                    pass
                # Small settle delay for lazy-loaded content
                self.page.wait_for_timeout(1500)
                return self.page.content()
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(2 + attempt * 2)
        raise RuntimeError(f"playwright failed for {url}: {last_err}")

    def download_image(self, url: str, dst: Path, referer: str | None = None) -> int:
        """Download via the same browser context (uses cf_clearance cookie)."""
        headers = {"Referer": referer or self.base_url}
        for attempt in range(3):
            try:
                resp = self.context.request.get(url, headers=headers, timeout=45000)
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}")
                body = resp.body()
                dst.write_bytes(body)
                return len(body)
            except Exception:  # noqa: BLE001
                time.sleep(1 + attempt)
        raise RuntimeError(f"failed to download {url}")


def download_gallery_pw(
    *,
    client: PWClient,
    site: str,
    title: str,
    source_url: str,
    image_urls: Iterable[str],
    out_dir: Path,
    referer: str | None = None,
):
    """Mirror of common.download_gallery but uses PWClient.download_image."""
    import json
    from dataclasses import asdict
    from scrapers.common import (
        GalleryManifest,
        MAX_IMAGES_PER_GALLERY,
        guess_ext,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    urls = list(image_urls)
    truncated = False
    if len(urls) > MAX_IMAGES_PER_GALLERY:
        urls = urls[:MAX_IMAGES_PER_GALLERY]
        truncated = True

    images: list[str] = []
    for idx, url in enumerate(urls, start=1):
        ext = guess_ext(url)
        fname = f"{idx:04d}{ext}"
        dst = out_dir / fname
        try:
            client.download_image(url, dst, referer=referer or source_url)
            images.append(fname)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] failed to download {url}: {e}")

    if not images:
        raise RuntimeError(f"no images downloaded for {source_url}")

    manifest = GalleryManifest(
        site=site,
        title=title,
        source_url=source_url,
        preview=images[0],
        image_count=len(images),
        images=images,
        truncated=truncated,
        scraped_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    manifest.write(out_dir)
    return manifest
