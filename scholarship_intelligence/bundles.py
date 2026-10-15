"""Save exact document bundles for later extraction jobs."""

from dataclasses import asdict, dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from scholarship_intelligence.settings import (
    PREPARED_SNAPSHOT_DIR,
    PROJECT_ROOT,
    RAW_SNAPSHOT_DIR,
    RUNTIME_DIR,
)


BUNDLE_VERSION = "bundle-v1"
_DOCUMENT_CHECKS = ContextVar('document_checks', default=None)


@contextmanager
def document_checks():
    """Validate shared immutable documents once within one read/workflow only."""
    if _DOCUMENT_CHECKS.get() is not None:
        yield
        return
    token = _DOCUMENT_CHECKS.set({})
    try:
        yield
    finally:
        _DOCUMENT_CHECKS.reset(token)


@dataclass(frozen=True)
class DocumentReference:
    snapshot_id: str
    source_url: str
    content_hash: str
    prepared_content_hash: str
    preparation_version: str
    raw_file_path: str
    prepared_file_path: str


@dataclass(frozen=True)
class BundleManifest:
    schema_version: str
    bundle_id: str
    offering_hint: str
    cycle_hint: str | None
    documents: tuple[DocumentReference, ...]
    bundle_file_path: str


def _resolve_inside(root, path, directory):
    """Resolve a path and reject references outside its expected directory."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()

    allowed_directory = root / directory.relative_to(PROJECT_ROOT)
    if not candidate.is_relative_to(allowed_directory):
        raise ValueError(f"File is outside the expected directory: {candidate}")

    return candidate


def _read_document(root, prepared_file_path):
    cache = _DOCUMENT_CHECKS.get()
    if cache is None:
        return _read_document_uncached(root, prepared_file_path)
    path = _resolve_inside(root, prepared_file_path, PREPARED_SNAPSHOT_DIR)
    key = (str(root), str(path))
    previous = cache.get(key)
    def stats(ref):
        return tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in
                     (root / ref.prepared_file_path, root / ref.raw_file_path))
    if previous is not None and stats(previous[0]) == previous[1]:
        return previous[0]
    reference = _read_document_uncached(root, path)
    cache[key] = (reference, stats(reference))
    return reference


def _read_document_uncached(root, prepared_file_path):
    prepared_path = _resolve_inside(
        root, prepared_file_path, PREPARED_SNAPSHOT_DIR
    )
    prepared_bytes = prepared_path.read_bytes()
    data = json.loads(prepared_bytes.decode("utf-8"))

    for field in (
        "snapshot_id",
        "source_url",
        "content_hash",
        "preparation_version",
        "raw_file_path",
        "prepared_file_path",
    ):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"Prepared document has an invalid {field}.")

    parsed_url = urlsplit(data["source_url"])
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        raise ValueError("Prepared document has an invalid source URL.")
    parsed_url.port

    blocks = data.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("Prepared document has no content blocks.")

    block_ids = []
    for block in blocks:
        if (
            not isinstance(block, dict)
            or not isinstance(block.get("block_id"), str)
            or not block["block_id"].strip()
            or not isinstance(block.get("text"), str)
        ):
            raise ValueError("Prepared document contains an invalid block.")
        block_ids.append(block["block_id"])

    if len(block_ids) != len(set(block_ids)):
        raise ValueError("Prepared document contains duplicate block IDs.")

    declared_prepared_path = _resolve_inside(
        root, data["prepared_file_path"], PREPARED_SNAPSHOT_DIR
    )
    if declared_prepared_path != prepared_path:
        raise ValueError("Prepared file does not match its declared path.")

    raw_path = _resolve_inside(root, data["raw_file_path"], RAW_SNAPSHOT_DIR)
    raw_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    if raw_hash != data["content_hash"]:
        raise ValueError("Raw document failed its integrity check.")

    expected_snapshot_id = hashlib.sha256(
        (data["source_url"] + "\0" + raw_hash).encode("utf-8")
    ).hexdigest()
    if data["snapshot_id"] != expected_snapshot_id:
        raise ValueError("Snapshot ID does not match the source/content version.")

    return DocumentReference(
        snapshot_id=data["snapshot_id"],
        source_url=data["source_url"],
        content_hash=raw_hash,
        prepared_content_hash=hashlib.sha256(prepared_bytes).hexdigest(),
        preparation_version=data["preparation_version"],
        raw_file_path=raw_path.relative_to(root).as_posix(),
        prepared_file_path=prepared_path.relative_to(root).as_posix(),
    )


def _identity_payload(offering_hint, cycle_hint, documents):
    return {
        "schema_version": BUNDLE_VERSION,
        "offering_hint": offering_hint,
        "cycle_hint": cycle_hint,
        "documents": [asdict(document) for document in documents],
    }


def _bundle_id(payload):
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _save_manifest(target, content):
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        if target.read_bytes() != content:
            raise ValueError("An existing bundle manifest failed its integrity check.")
        return

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=".bundle-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)

        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def create_bundle(
    prepared_file_paths: list[str | Path],
    offering_hint: str,
    cycle_hint: str | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
) -> BundleManifest:
    """Save references to selected documents for one proposed offering."""
    root = Path(project_root).resolve()

    if not isinstance(offering_hint, str) or not offering_hint.strip():
        raise ValueError("An offering hint is required.")
    offering_hint = offering_hint.strip()

    if cycle_hint is not None:
        if not isinstance(cycle_hint, str) or not cycle_hint.strip():
            raise ValueError("Cycle hint must be a nonempty string or None.")
        cycle_hint = cycle_hint.strip()

    if not prepared_file_paths:
        raise ValueError("A bundle requires at least one prepared document.")

    documents = tuple(
        sorted(
            (_read_document(root, path) for path in prepared_file_paths),
            key=lambda document: document.snapshot_id,
        )
    )

    snapshot_ids = [document.snapshot_id for document in documents]
    if len(snapshot_ids) != len(set(snapshot_ids)):
        raise ValueError(
            "A bundle cannot contain duplicate snapshots or two preparations "
            "of the same snapshot."
        )

    payload = _identity_payload(offering_hint, cycle_hint, documents)
    bundle_id = _bundle_id(payload)
    bundle_directory = root / RUNTIME_DIR.relative_to(PROJECT_ROOT) / "bundles"
    target = bundle_directory / f"{bundle_id}.json"

    manifest = BundleManifest(
        schema_version=BUNDLE_VERSION,
        bundle_id=bundle_id,
        offering_hint=offering_hint,
        cycle_hint=cycle_hint,
        documents=documents,
        bundle_file_path=target.relative_to(root).as_posix(),
    )
    content = (
        json.dumps(asdict(manifest), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    _save_manifest(target, content)
    return manifest


def load_bundle(
    bundle_file_path: str | Path,
    *,
    project_root: Path = PROJECT_ROOT,
) -> BundleManifest:
    """Load a manifest and verify that its referenced files remain unchanged."""
    root = Path(project_root).resolve()
    bundle_directory = RUNTIME_DIR / "bundles"
    target = _resolve_inside(root, bundle_file_path, bundle_directory)
    data = json.loads(target.read_text(encoding="utf-8"))

    if data.get("schema_version") != BUNDLE_VERSION:
        raise ValueError("Unsupported bundle version.")

    offering_hint = data.get("offering_hint")
    cycle_hint = data.get("cycle_hint")
    if not isinstance(offering_hint, str) or not offering_hint.strip():
        raise ValueError("Bundle has an invalid offering hint.")
    if cycle_hint is not None and (
        not isinstance(cycle_hint, str) or not cycle_hint.strip()
    ):
        raise ValueError("Bundle has an invalid cycle hint.")

    document_data = data.get("documents")
    if not isinstance(document_data, list) or not document_data:
        raise ValueError("Bundle has no document references.")

    try:
        documents = tuple(DocumentReference(**item) for item in document_data)
    except TypeError as error:
        raise ValueError("Bundle has malformed document references.") from error

    snapshot_ids = [document.snapshot_id for document in documents]
    if len(snapshot_ids) != len(set(snapshot_ids)):
        raise ValueError("Bundle contains duplicate snapshots.")
    if documents != tuple(sorted(documents, key=lambda item: item.snapshot_id)):
        raise ValueError("Bundle documents are not in their canonical order.")

    payload = _identity_payload(offering_hint, cycle_hint, documents)
    expected_id = _bundle_id(payload)
    if data.get("bundle_id") != expected_id:
        raise ValueError("Bundle manifest fingerprint does not match.")

    expected_path = (
        root / RUNTIME_DIR.relative_to(PROJECT_ROOT)
        / "bundles" / f"{expected_id}.json"
    )
    if target != expected_path:
        raise ValueError("Bundle filename does not match its identifier.")
    if data.get("bundle_file_path") != target.relative_to(root).as_posix():
        raise ValueError("Bundle manifest has an inconsistent file path.")

    for document in documents:
        current = _read_document(root, document.prepared_file_path)
        if current != document:
            raise ValueError(
                f"Bundled document changed: {document.prepared_file_path}"
            )

    return BundleManifest(
        schema_version=BUNDLE_VERSION,
        bundle_id=expected_id,
        offering_hint=offering_hint,
        cycle_hint=cycle_hint,
        documents=documents,
        bundle_file_path=target.relative_to(root).as_posix(),
    )
