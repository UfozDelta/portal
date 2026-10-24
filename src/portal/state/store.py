"""Atomic read/write of ~/.portal/state.json.

No secrets are ever stored here: not the Cloudflare API token, not the tunnel
token. Only ids and paths.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import state_path
from ..errors import StateError

SCHEMA_VERSION = 1
LOCK_TIMEOUT = 15.0
# A lock older than this is from a process that died mid-write; holding the
# lock only spans one read-modify-write, which takes milliseconds.
LOCK_STALE_AFTER = 60.0


@dataclass
class ProjectRecord:
    slug: str
    source_path: str
    compose_file: str
    override_file: str
    service: str
    container_name: str
    port: int
    fqdn: str
    tunnel_id: str
    zone_id: str
    dns_record_id: str
    network: str
    cloudflared_container: str
    deployed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    # e.g. "ssh://user@1.2.3.4" when this project runs on a remote docker
    # daemon. Empty means local. Kept so status()/destroy() automatically
    # target the same machine deploy() used, without having to repeat it.
    docker_host: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class StateStore:
    """A tiny JSON store keyed by project slug."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or state_path()

    def _load_raw(self) -> dict:
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "projects": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"Could not read state file {self.path}: {exc}") from None
        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("projects", {})
        return data

    def _write_raw(self, data: dict) -> None:
        """Write to a temp file in the same directory, then rename over the
        target. os.replace is atomic on both POSIX and Windows."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".state-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def list(self) -> list[ProjectRecord]:
        raw = self._load_raw()["projects"]
        return [ProjectRecord.from_dict(v) for v in raw.values()]

    def get(self, slug: str) -> ProjectRecord | None:
        raw = self._load_raw()["projects"].get(slug)
        return ProjectRecord.from_dict(raw) if raw else None

    def require(self, slug: str) -> ProjectRecord:
        record = self.get(slug)
        if record is None:
            raise StateError(
                f"No deployment named '{slug}' in {self.path}. "
                f"status() lists what portal knows about."
            )
        return record

    @contextmanager
    def _locked(self):
        """Serialize read-modify-write across processes, so two deploys running
        at once can't each load the file, add their entry, and have the second
        write drop the first. A lockfile created with O_EXCL works the same on
        POSIX and Windows."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > LOCK_STALE_AFTER:
                        lock.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise StateError(
                        f"Timed out waiting for {lock}. If no other portal process "
                        f"is running, delete that file."
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass

    def put(self, record: ProjectRecord) -> None:
        with self._locked():
            data = self._load_raw()
            data["projects"][record.slug] = record.to_dict()
            self._write_raw(data)

    def delete(self, slug: str) -> bool:
        with self._locked():
            data = self._load_raw()
            existed = data["projects"].pop(slug, None) is not None
            if existed:
                self._write_raw(data)
            return existed
