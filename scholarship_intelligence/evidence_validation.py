"""Deterministic source/evidence checks, not factual verification."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from pydantic import BaseModel, HttpUrl

from scholarship_intelligence.bundles import load_bundle
from scholarship_intelligence.models import Claim, ScholarshipExtraction
from scholarship_intelligence.settings import PROJECT_ROOT


VALIDATOR_VERSION = "evidence-validator-v4"
PDF_VALIDATOR_VERSION = "evidence-validator-v4"


@dataclass(frozen=True)
class EvidenceIssue:
    code: str
    message: str
    evidence_id: str | None = None
    field_path: str | None = None


@dataclass(frozen=True)
class EvidenceReport:
    bundle_id: str
    documents_checked: int
    evidence_checked: int
    exact_quote_matches: int
    url_claims_checked: int
    issues: tuple[EvidenceIssue, ...]
    validator_version: str = VALIDATOR_VERSION

    @property
    def evidence_checks_passed(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence_checks_passed"] = self.evidence_checks_passed
        result["factual_verification"] = "pending"
        result["critical_checks_passed"] = False
        result["verified"] = False
        return result


def _path_exists(value: Any, path: str) -> bool:
    """Resolve dotted fields/indices and bracketed array indices, without eval.

    Both benefits.value.0.description and benefits.value[0].description
    identify the same field. Brackets permit only nonnegative array indices;
    wildcards, quoted keys, expressions and partial paths are rejected.
    """
    if not isinstance(path, str) or not path:
        return False
    current = value
    for segment in path.split("."):
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*|[0-9]+)((?:\[[0-9]+\])*)", segment)
        if match is None:
            return False
        name, brackets = match.groups()
        if isinstance(current, dict) and name in current:
            current = current[name]
        elif isinstance(current, list) and name.isascii() and name.isdigit():
            try:
                index = int(name)
            except ValueError:
                return False
            if index >= len(current):
                return False
            current = current[index]
        else:
            return False
        for token in re.findall(r"\[([0-9]+)\]", brackets):
            if not isinstance(current, list):
                return False
            try:
                index = int(token)
            except ValueError:
                return False
            if index >= len(current):
                return False
            current = current[index]
    return True


def _url(value: Any) -> str | None:
    try:
        return str(HttpUrl(value))
    except (ValueError, TypeError):
        return None


def validate_evidence(
    candidate: ScholarshipExtraction,
    bundle_file_path: str | Path,
    *,
    project_root: Path = PROJECT_ROOT,
) -> EvidenceReport:
    """Check references, exact quotes and URL provenance against saved bytes.

    Invalid structure, changed source files or a different bundle raise an
    error. Evidence problems return issues. Passing these checks does not
    establish completeness, semantic support or scholarship eligibility.
    """
    # Streamlit can reload models while a worker still holds the previous
    # class object. Accept the same named Pydantic model across reloads, then
    # fully revalidate its payload against this validator's canonical schema.
    candidate_type = type(candidate)
    same_model = (isinstance(candidate, BaseModel)
                  and candidate_type.__module__ == ScholarshipExtraction.__module__
                  and candidate_type.__qualname__ == ScholarshipExtraction.__qualname__)
    if not isinstance(candidate, ScholarshipExtraction) and not same_model:
        raise TypeError("A ScholarshipExtraction candidate is required.")
    candidate = ScholarshipExtraction.model_validate(candidate.model_dump())
    root = Path(project_root).resolve()
    bundle = load_bundle(bundle_file_path, project_root=root)
    if candidate.bundle_id != bundle.bundle_id:
        raise ValueError("Candidate bundle identity does not match.")
    if candidate.snapshot_ids != [ref.snapshot_id for ref in bundle.documents]:
        raise ValueError("Candidate snapshot identities or order do not match.")

    prepared_root = (root / "data/runtime/snapshots/prepared").resolve()
    documents = {}
    for ref in bundle.documents:
        path = (root / ref.prepared_file_path).resolve()
        if not path.is_relative_to(prepared_root):
            raise ValueError("Prepared file is outside prepared storage.")
        content = path.read_bytes()
        if sha256(content).hexdigest() != ref.prepared_content_hash:
            raise ValueError("Prepared source changed during evidence validation.")
        document = json.loads(content.decode("utf-8"))
        if any(document.get(key) != getattr(ref, key) for key in (
            "snapshot_id", "source_url", "content_hash", "preparation_version"
        )):
            raise ValueError("Prepared source metadata does not match its bundle.")
        is_pdf = ref.preparation_version.startswith("pdf-text-")
        if not is_pdf and not ref.preparation_version.startswith("html-"):
            raise ValueError("Unsupported prepared document format.")
        blocks = document.get("blocks")
        if not isinstance(blocks, list):
            raise ValueError("Prepared source has no block list.")
        indexed = {block["block_id"]: block for block in blocks}
        if len(indexed) != len(blocks):
            raise ValueError("Prepared source contains duplicate block IDs.")
        if is_pdf:
            count = document.get("page_count")
            numbers = [block.get("page_number") for block in blocks]
            if (type(count) is not int or count < 1 or len(numbers) != count
                    or any(type(number) is not int for number in numbers)
                    or sorted(numbers) != list(range(1, count + 1))):
                raise ValueError("Prepared PDF has invalid or incomplete physical page locations.")
        documents[ref.snapshot_id] = (document, indexed)

    issues = []
    quote_matches = 0
    cited_blocks = {}
    candidate_data = candidate.model_dump()
    claim_fields = {
        name for name in type(candidate).model_fields
        if isinstance(getattr(candidate, name), Claim)
    }
    for evidence in candidate.evidence:
        document, indexed = documents[evidence.snapshot_id]
        located = []
        if len(evidence.location.block_ids) != len(set(evidence.location.block_ids)):
            issues.append(EvidenceIssue("duplicate_block", "Repeated source block ID.", evidence.evidence_id))
        for block_id in evidence.location.block_ids:
            block = indexed.get(block_id)
            if block is None:
                issues.append(EvidenceIssue("missing_block", f"Unknown block: {block_id}.", evidence.evidence_id))
            else:
                located.append(block)
        cited_blocks[evidence.evidence_id] = (document, located)
        is_pdf = document["preparation_version"].startswith("pdf-text-")
        if is_pdf and (evidence.location.page_number is None or not located
                       or any(block["page_number"] != evidence.location.page_number for block in located)):
            issues.append(EvidenceIssue("pdf_page_number", "PDF evidence must identify the physical page of every cited block.", evidence.evidence_id))
        elif not is_pdf and evidence.location.page_number is not None:
            issues.append(EvidenceIssue("html_page_number", "HTML evidence must not have a PDF page number.", evidence.evidence_id))
        if any(
            evidence.quote in text
            for block in located
            for text in [block["text"], *[link.get("text", "") for link in block.get("links", [])]]
        ):
            quote_matches += 1
        else:
            issues.append(EvidenceIssue("quote_mismatch", "Quote is not an exact substring of a cited block or link text.", evidence.evidence_id))
        if evidence.location.section is not None and not any(
            evidence.location.section in block.get("heading_path", [])
            or evidence.location.section == " > ".join(block.get("heading_path", []))
            for block in located
        ):
            issues.append(EvidenceIssue("section_mismatch", "Section does not match the cited heading context.", evidence.evidence_id))
        for field_path in evidence.field_paths:
            if field_path.split(".", 1)[0] not in claim_fields or not _path_exists(candidate_data, field_path):
                issues.append(EvidenceIssue("invalid_field_path", "Evidence field path does not identify an existing claim field.", evidence.evidence_id, field_path))

    url_checks = 0
    for field in ("official_source", "application_url"):
        claim = getattr(candidate, field)
        if claim.value is None:
            continue
        url_checks += 1
        target = _url(claim.value)
        available = set()
        for evidence_id in claim.evidence_ids:
            document, located = cited_blocks[evidence_id]
            if located and field == "official_source":
                available.add(_url(document["source_url"]))
            for block in located:
                available.update(_url(link.get("url")) for link in block.get("links", []))
        if target is None or target not in available:
            issues.append(EvidenceIssue("unsupported_url", "URL is absent from the claim's cited source URLs and link destinations.", field_path=field))

    return EvidenceReport(
        bundle_id=bundle.bundle_id,
        documents_checked=len(documents),
        evidence_checked=len(candidate.evidence),
        exact_quote_matches=quote_matches,
        url_claims_checked=url_checks,
        issues=tuple(issues),
        validator_version=PDF_VALIDATOR_VERSION if any(ref.preparation_version.startswith("pdf-text-") for ref in bundle.documents) else VALIDATOR_VERSION,
    )
