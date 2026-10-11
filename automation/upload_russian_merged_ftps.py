#!/usr/bin/env python3
"""Upload the merged Russian package (single SQL zip + rollback zip) over FTPS."""
from __future__ import annotations
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from upload_city_batches_ftps import connect  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MERGED = ROOT / "artifacts" / "russian-article-queue" / "merged"
STATE = MERGED / "ftps-upload-state.json"
FILES = ["russian-all-articles.zip", "russian-all-articles-rollback.zip"]


def main() -> int:
    state = {"files": {}}
    ftp = connect()
    try:
        for name in FILES:
            path = MERGED / name
            size = path.stat().st_size
            temp = f".{name}.uploading"
            with path.open("rb") as stream:
                ftp.storbinary(f"STOR {temp}", stream, blocksize=1024 * 1024)
            if ftp.size(temp) != size:
                ftp.delete(temp)
                raise RuntimeError(f"size verification failed for {name}")
            try:
                ftp.delete(name)
            except Exception:
                pass
            ftp.rename(temp, name)
            state["files"][name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": size,
                                    "uploaded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
            print(f"Uploaded merged Russian package: {name} ({size} bytes)", flush=True)
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"uploaded": len(state["files"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
