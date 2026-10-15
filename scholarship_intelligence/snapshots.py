"""Preserve original response bytes for later evidence inspection."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from scholarship_intelligence.settings import PROJECT_ROOT, RAW_SNAPSHOT_DIR


@dataclass(frozen=True)
class RawSnapshot:
    source_url: str
    retrieved_at: str
    content_hash: str
    raw_file_path: str
    content_type: str | None
    reused: bool


def save_raw_snapshot(
    source_url: str,
    content: bytes,
    retrieved_at: datetime,
    content_type: str | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
) -> RawSnapshot:
    """Save one source version, reusing identical bytes for the same URL."""
    parsed_url = urlsplit(source_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ValueError("Source URL must be an absolute HTTP or HTTPS URL.")

    if not isinstance(content, bytes):
        raise TypeError("Content must be bytes.")

    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise ValueError("Retrieval time must include a timezone.")

    root = Path(project_root).resolve()
    raw_subdirectory = RAW_SNAPSHOT_DIR.relative_to(PROJECT_ROOT)

    # Use the exact fetched URL. URL normalization will be handled separately.
    source_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    content_hash = hashlib.sha256(content).hexdigest()

    target = root / raw_subdirectory / source_hash / f"{content_hash}.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    reused = target.exists()

    if reused:
        saved_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if saved_hash != content_hash:
            raise ValueError(f"Saved snapshot failed integrity check: {target}")
    else:
        temporary_path = None
        try:
            # Write beside the target so replacement stays on the same drive.
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=".snapshot-",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content)

            os.replace(temporary_path, target)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    return RawSnapshot(
        source_url=source_url,
        retrieved_at=retrieved_at.astimezone(timezone.utc).isoformat(),
        content_hash=content_hash,
        raw_file_path=target.relative_to(root).as_posix(),
        content_type=content_type,
        reused=reused,
    )