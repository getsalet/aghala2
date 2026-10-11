#!/usr/bin/env python3
"""Upload new or changed Russian 50-article ZIP packages over FTPS."""
from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone
from pathlib import Path
from upload_city_batches_ftps import connect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "russian-article-queue"
PACKAGES = OUT / "packages"
STATE = OUT / "ftps-upload-state.json"


def load(path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, dict) else default


def pending_packages():
    tracked = load(STATE, {"files": {}}).get("files", {})
    rows = []
    for record in load(PACKAGES / "manifest.json", {"packages": []}).get("packages", []):
        path = PACKAGES / record["zip"]
        if not path.exists():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if tracked.get(path.name, {}).get("sha256") != digest:
            rows.append((path, digest, path.stat().st_size))
    return rows


def main() -> int:
    pending = pending_packages()
    if not pending:
        print(json.dumps({"uploaded": 0}))
        return 0
    state = load(STATE, {"files": {}}); tracked = state.setdefault("files", {})
    ftp = connect(); uploaded = 0
    try:
        for path, digest, size in pending:
            temp = f".{path.name}.uploading"
            with path.open("rb") as stream:
                ftp.storbinary(f"STOR {temp}", stream, blocksize=1024 * 1024)
            if ftp.size(temp) != size:
                ftp.delete(temp)
                raise RuntimeError(f"size verification failed for {path.name}")
            try:
                ftp.delete(path.name)
            except Exception:
                pass
            ftp.rename(temp, path.name)
            tracked[path.name] = {"sha256": digest, "size": size, "uploaded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
            uploaded += 1
            print(f"Uploaded Russian package: {path.name} ({size} bytes)", flush=True)
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    print(json.dumps({"uploaded": uploaded}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
