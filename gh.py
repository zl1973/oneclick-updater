"""GitHub Releases 查询（绿色版程序的“官网最新版”来源）。"""
import fnmatch
import json
import os
import urllib.request

UA = "OneKeyUpdater/0.1"


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/vnd.github+json",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def latest_release(repo):
    """repo 形如 'obsidianmd/obsidian-releases'。"""
    return _get("https://api.github.com/repos/%s/releases/latest" % repo.strip().strip("/"))


def latest_tag(repo):
    try:
        rel = latest_release(repo)
        return (rel.get("tag_name") or rel.get("name") or "").strip()
    except Exception:
        return ""


def pick_asset(release, pattern="*"):
    assets = release.get("assets") or []
    if not assets:
        return None
    pattern = (pattern or "*").lower()
    for a in assets:
        if fnmatch.fnmatch((a.get("name") or "").lower(), pattern):
            return a
    return assets[0]
