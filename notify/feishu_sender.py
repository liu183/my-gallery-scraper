"""Feishu (Lark) message sender.

Capabilities:
- Acquire tenant_access_token from app_id/app_secret
- Upload image to Feishu and obtain image_key
- Send interactive card (cover preview) via incoming webhook
- Send image message via incoming webhook using image_key

Each site has its own bot config:
    FeishuBot(app_id, app_secret, webhook_url, webhook_secret?)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type


FEISHU_OPEN_API = "https://open.feishu.cn/open-apis"


class FeishuError(RuntimeError):
    pass


@dataclass
class FeishuBot:
    site: str
    app_id: str
    app_secret: str
    webhook_url: str
    webhook_secret: str = ""  # optional, for signed webhook


def _gen_sign(secret: str, timestamp: int) -> str:
    """Feishu webhook signature."""
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(h).decode("utf-8")


class FeishuClient:
    def __init__(self, bot: FeishuBot) -> None:
        self.bot = bot
        self._token: str | None = None
        self._token_exp: float = 0
        self._http = httpx.Client(timeout=60.0)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "FeishuClient":
        return self

    def __exit__(self, *a) -> None:
        self.close()

    # ---------- auth ----------
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, FeishuError)),
        reraise=True,
    )
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self._http.post(
            f"{FEISHU_OPEN_API}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.bot.app_id, "app_secret": self.bot.app_secret},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise FeishuError(f"token error: {data}")
        self._token = data["tenant_access_token"]
        self._token_exp = time.time() + int(data.get("expire", 7200))
        return self._token

    # ---------- image upload ----------
    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, FeishuError)),
        reraise=True,
    )
    def upload_image(self, path: Path) -> str:
        """Upload a local image, return image_key."""
        token = self._get_token()
        with open(path, "rb") as f:
            files = {"image": (path.name, f, "application/octet-stream")}
            data = {"image_type": "message"}
            r = self._http.post(
                f"{FEISHU_OPEN_API}/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                files=files,
                data=data,
            )
        r.raise_for_status()
        resp = r.json()
        if resp.get("code") != 0:
            raise FeishuError(f"upload failed for {path.name}: {resp}")
        return resp["data"]["image_key"]

    # ---------- webhook send ----------
    def _webhook_post(self, payload: dict) -> dict:
        body = dict(payload)
        if self.bot.webhook_secret:
            ts = int(time.time())
            body["timestamp"] = str(ts)
            body["sign"] = _gen_sign(self.bot.webhook_secret, ts)
        r = self._http.post(self.bot.webhook_url, json=body)
        r.raise_for_status()
        resp = r.json()
        code = resp.get("code", resp.get("StatusCode"))
        if code not in (0, None):
            raise FeishuError(f"webhook error: {resp}")
        return resp

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, FeishuError)),
        reraise=True,
    )
    def send_card(
        self,
        *,
        title: str,
        source_url: str,
        image_count: int,
        preview_image_key: str,
        site: str,
    ) -> None:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": f"📷 {title[:140]}"},
            },
            "elements": [
                {
                    "tag": "img",
                    "img_key": preview_image_key,
                    "alt": {"tag": "plain_text", "content": title[:80]},
                },
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**站点**\n{site}"},
                        },
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md", "content": f"**图片数**\n{image_count}"},
                        },
                    ],
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看原页面"},
                            "type": "primary",
                            "url": source_url,
                        }
                    ],
                },
            ],
        }
        self._webhook_post({"msg_type": "interactive", "card": card})

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, FeishuError)),
        reraise=True,
    )
    def send_image(self, image_key: str) -> None:
        self._webhook_post({"msg_type": "image", "content": {"image_key": image_key}})

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, FeishuError)),
        reraise=True,
    )
    def send_text(self, text: str) -> None:
        self._webhook_post({"msg_type": "text", "content": {"text": text}})


def load_bot_from_env(site: str, env: dict) -> FeishuBot | None:
    """Read bot credentials for a given site from env (case-insensitive key)."""
    key = site.upper().replace("-", "_")
    app_id = env.get(f"FEISHU_{key}_APP_ID")
    app_secret = env.get(f"FEISHU_{key}_APP_SECRET")
    webhook = env.get(f"FEISHU_{key}_WEBHOOK")
    secret = env.get(f"FEISHU_{key}_WEBHOOK_SECRET", "")
    if not (app_id and app_secret and webhook):
        return None
    return FeishuBot(
        site=site,
        app_id=app_id,
        app_secret=app_secret,
        webhook_url=webhook,
        webhook_secret=secret,
    )
