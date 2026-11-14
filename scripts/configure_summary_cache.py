"""Provision encrypted cache credentials and migrate local checkpoints. Never prints secrets.

Run locally after deploying cache/wrangler.jsonc: --configure then --seed.
Requires authenticated Wrangler and gh CLIs. Does not generate summaries.
"""

import argparse
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from check_story_summary import load_config
from remote_summary_cache import RemoteSummaryCache
from summary_cache import SummaryCache

URL = "https://ai-daily-brief-summary-cache.ai-daily-brief.workers.dev"
REPOSITORY = "rahulprasath-s/AI-Daily-Brief"


def command(arguments, value=None):
    result = subprocess.run(arguments, input=value, text=True, capture_output=True, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(f"Credential configuration failed: {arguments[0]} {arguments[1]}; inspect local CLI authentication.")


def configure():
    env = load_config()
    token = env.SUMMARY_CACHE_TOKEN or secrets.token_urlsafe(32)
    if not env.NVIDIA_NIM_API_KEY:
        raise ValueError("Missing local NVIDIA_NIM_API_KEY")
    # Save locally first so interrupted provisioning can reuse the same token.
    path = ROOT / ".dev.vars"
    lines = [line for line in path.read_text().splitlines()
             if line.split("=", 1)[0].strip().removeprefix("export ") not in {"SUMMARY_CACHE_URL", "SUMMARY_CACHE_TOKEN"}]
    lines.extend([f"SUMMARY_CACHE_URL={shlex.quote(URL)}", f"SUMMARY_CACHE_TOKEN={shlex.quote(token)}"])
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    command([str(ROOT / "node_modules/.bin/wrangler"), "secret", "put", "SUMMARY_CACHE_TOKEN", "--config", "cache/wrangler.jsonc"], token)
    for key, value in {"SUMMARY_CACHE_TOKEN": token, "NVIDIA_NIM_API_KEY": env.NVIDIA_NIM_API_KEY}.items():
        command(["gh", "secret", "set", key, "--repo", REPOSITORY], value)
    command(["gh", "variable", "set", "SUMMARY_CACHE_URL", "--repo", REPOSITORY], URL)
    print("Encrypted Worker/GitHub credentials configured; active local NIM key synced. No secret values printed.")


def seed():
    env = load_config()
    local = SummaryCache(ROOT / ".summary-cache")
    remote = RemoteSummaryCache(ROOT / ".summary-cache", env.SUMMARY_CACHE_URL, env.SUMMARY_CACHE_TOKEN)
    try:
        rows = local.connection.execute("SELECT key, payload FROM results").fetchall()
        for key, payload in rows:
            remote.set(key, json.loads(payload))
        print(f"Migrated {len(rows)} local checkpoints to private persistent storage; no NIM calls.")
    finally:
        local.close()
        remote.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    if args.configure:
        configure()
    if args.seed:
        seed()
