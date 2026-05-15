"""Notify dispatcher.

Runs every 5 hours. Picks up to N (default 3) unsent gallery artifacts from
the current repo, downloads them via GitHub REST API, sends each to its
site-specific Feishu bot, then marks them in state/sent.json.

Failures are recorded in state/failed.json for visibility (no auto retry,
since the artifact still exists; next run will pick it up again because it's
not in sent.json).

Environment variables required:
  GITHUB_TOKEN           - automatic in actions; needs actions:read
  GITHUB_REPOSITORY      - e.g. "user/repo"
  FEISHU_<SITE>_APP_ID
  FEISHU_<SITE>_APP_SECRET
  FEISHU_<SITE>_WEBHOOK
  FEISHU_<SITE>_WEBHOOK_SECRET (optional)
   where <SITE> ∈ {EVERIACLUB, BESTGIRLSEXY, EROPURU, GEINOU_NUDE}

Optional:
  NOTIFY_BATCH_SIZE      - default 3
  IMAGE_SEND_INTERVAL    - seconds between image messages, default 1.0
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from notify.feishu_sender import FeishuClient, load_bot_from_env

GITHUB_API = "https://api.github.com"
SENT_FILE = Path("state/sent.json")
FAILED_FILE = Path("state/failed.json")
WORK_DIR = Path("notify_work")

ARTIFACT_PREFIX = "gallery-"
DEFAULT_BATCH = int(os.environ.get("NOTIFY_BATCH_SIZE", "3"))
IMAGE_INTERVAL = float(os.environ.get("IMAGE_SEND_INTERVAL", "1.0"))


def gh_headers() -> dict:
    token = os.environ["GITHUB_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def list_artifacts(repo: str) -> list[dict]:
    """List all gallery-* artifacts that haven't expired, oldest first."""
    out: list[dict] = []
    with httpx.Client(timeout=60.0, headers=gh_headers()) as client:
        page = 1
        while True:
            r = client.get(
                f"{GITHUB_API}/repos/{repo}/actions/artifacts",
                params={"per_page": 100, "page": page},
            )
            r.raise_for_status()
            data = r.json()
            arts = data.get("artifacts", [])
            if not arts:
                break
            out.extend(arts)
            if len(arts) < 100:
                break
            page += 1
    arts = [
        a for a in out
        if not a.get("expired") and a.get("name", "").startswith(ARTIFACT_PREFIX)
    ]
    # Oldest first
    arts.sort(key=lambda a: a.get("created_at", ""))
    return arts


def download_artifact(repo: str, artifact: dict, dst: Path) -> Path:
    """Download and unzip artifact to dst directory. Returns dst."""
    dst.mkdir(parents=True, exist_ok=True)
    url = f"{GITHUB_API}/repos/{repo}/actions/artifacts/{artifact['id']}/zip"
    with httpx.Client(timeout=300.0, headers=gh_headers(), follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        zf.extractall(dst)
    return dst


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_artifact_site(name: str) -> str | None:
    """gallery-<site>-<timestamp>-<slug>  →  <site>"""
    parts = name.split("-")
    if len(parts) >= 3 and parts[0] == "gallery":
        # site may itself contain underscores (geinou_nude) but artifact name uses dashes
        # convention: site segment is parts[1]; for geinou_nude, allow "geinou_nude"
        return parts[1]
    return None


def find_manifest(root: Path) -> Path | None:
    # manifest.json should be at the artifact root
    direct = root / "manifest.json"
    if direct.exists():
        return direct
    for p in root.rglob("manifest.json"):
        return p
    return None


def send_one_gallery(artifact: dict, repo: str, env: dict) -> tuple[bool, str]:
    name = artifact["name"]
    site = parse_artifact_site(name)
    if not site:
        return False, f"cannot parse site from name {name}"
    bot = load_bot_from_env(site, env)
    if not bot:
        return False, f"missing Feishu credentials for site={site}"

    work = WORK_DIR / str(artifact["id"])
    if work.exists():
        # clean previous attempt
        for p in work.rglob("*"):
            if p.is_file():
                p.unlink()
    work.mkdir(parents=True, exist_ok=True)
    try:
        download_artifact(repo, artifact, work)
    except Exception as e:  # noqa: BLE001
        return False, f"artifact download failed: {e}"

    manifest_path = find_manifest(work)
    if not manifest_path:
        return False, "manifest.json not found inside artifact"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    title = manifest.get("title") or "Untitled"
    source_url = manifest.get("source_url") or ""
    images = manifest.get("images") or []
    preview = manifest.get("preview") or (images[0] if images else None)
    if not preview:
        return False, "no preview image in manifest"
    if not images:
        return False, "no images in manifest"

    preview_path = base / preview
    if not preview_path.exists():
        return False, f"preview file missing: {preview}"

    with FeishuClient(bot) as fc:
        try:
            preview_key = fc.upload_image(preview_path)
        except Exception as e:  # noqa: BLE001
            return False, f"preview upload failed: {e}"

        try:
            fc.send_card(
                title=title,
                source_url=source_url,
                image_count=len(images),
                preview_image_key=preview_key,
                site=site,
            )
        except Exception as e:  # noqa: BLE001
            return False, f"card send failed: {e}"

        sent = 0
        for fname in images:
            fpath = base / fname
            if not fpath.exists():
                print(f"[warn] missing image: {fname}")
                continue
            try:
                key = fc.upload_image(fpath)
                fc.send_image(key)
                sent += 1
            except Exception as e:  # noqa: BLE001
                print(f"[warn] image {fname} failed: {e}")
            time.sleep(IMAGE_INTERVAL)

        try:
            fc.send_text(f"✅ 完毕：{title}（共 {sent}/{len(images)} 张已发送）")
        except Exception:  # noqa: BLE001
            pass

    return True, f"sent {sent}/{len(images)} images"


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    env = dict(os.environ)
    batch = DEFAULT_BATCH

    sent_state = load_json(SENT_FILE, {"sent": []})
    failed_state = load_json(FAILED_FILE, {"failed": []})
    sent_ids = {str(x.get("artifact_id")) for x in sent_state.get("sent", [])}

    artifacts = list_artifacts(repo)
    candidates = [a for a in artifacts if str(a["id"]) not in sent_ids]
    print(f"[info] total artifacts={len(artifacts)} unsent={len(candidates)}")

    picked = candidates[:batch]
    if not picked:
        print("[info] nothing to send")
        return 0

    for art in picked:
        print(f"[info] sending artifact id={art['id']} name={art['name']}")
        ok, msg = send_one_gallery(art, repo, env)
        record = {
            "artifact_id": art["id"],
            "artifact_name": art["name"],
            "at": datetime.now(timezone.utc).isoformat(),
            "message": msg,
        }
        if ok:
            sent_state.setdefault("sent", []).append(record)
            print(f"[ok] {art['name']}: {msg}")
        else:
            failed_state.setdefault("failed", []).append(record)
            print(f"[fail] {art['name']}: {msg}")

    # Trim sent state to last 1000 entries
    if len(sent_state.get("sent", [])) > 1000:
        sent_state["sent"] = sent_state["sent"][-1000:]
    if len(failed_state.get("failed", [])) > 500:
        failed_state["failed"] = failed_state["failed"][-500:]

    save_json(SENT_FILE, sent_state)
    save_json(FAILED_FILE, failed_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
