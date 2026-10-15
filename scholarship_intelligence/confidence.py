"""Approved evidence-based scoring; pending assessments are not scores."""

from dataclasses import asdict, dataclass
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Literal

from scholarship_intelligence.models import ScholarshipExtraction
from scholarship_intelligence.semantic_assessment import (
    ASSESSMENT_VERSION, CHECK_DEFINITIONS, SemanticAssessment,
    validate_assessment, candidate_fingerprint,
)


POLICY_VERSION = "confidence-policy-v1"
METRIC_WEIGHTS = (
    ("authenticity", 30),
    ("information_support", 50),
    ("currency_consistency", 20),
)
VERIFIED_THRESHOLD = Decimal("95")
CRITICAL_CAP = Decimal("94")


def _fingerprint(value: Any) -> str:
    content = json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return sha256(content).hexdigest()


def policy_fingerprint() -> str:
    return _fingerprint({
        "policy_version": POLICY_VERSION,
        "assessment_version": ASSESSMENT_VERSION,
        "weights": METRIC_WEIGHTS,
        "check_definitions": CHECK_DEFINITIONS,
        "check_scores": {"pass": 100, "fail": 0},
        "pending_rule": "metric_and_total_unassigned",
        "not_applicable_rule": "exclude_from_group_denominator",
        "empty_group_rule": "unassigned; no weight redistribution",
        "verified_threshold": str(VERIFIED_THRESHOLD),
        "critical_cap": str(CRITICAL_CAP),
        "comparison_rule": "unrounded Decimal values",
    })


@dataclass(frozen=True)
class MetricResult:
    group: str
    weight: int
    score: Decimal | None
    passed: int
    failed: int
    pending: int
    not_applicable: int


@dataclass(frozen=True)
class ConfidenceResult:
    bundle_id: str
    candidate_fingerprint: str
    assessment_fingerprint: str
    policy_fingerprint: str
    metrics: tuple[MetricResult, ...]
    raw_confidence: Decimal | None
    confidence_score: Decimal | None
    verification_status: Literal["VERIFIED", "REVIEW_REQUIRED"]
    critical_checks_passed: bool
    blockers: tuple[str, ...]
    withheld_fields: tuple[str, ...]
    last_verified_at: str | None
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        def render(value):
            if isinstance(value, Decimal):
                return float(value.quantize(Decimal("0.01")))
            if isinstance(value, dict):
                return {key: render(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [render(item) for item in value]
            return value

        result = render(asdict(self))
        result["pending_metric_groups"] = [
            metric.group for metric in self.metrics if metric.score is None
        ]
        return result


def score_candidate(
    candidate: ScholarshipExtraction,
    assessment: SemanticAssessment,
) -> ConfidenceResult:
    """Score existing review outcomes without repeating source checks.

    Each applicable check contributes 100 for pass or 0 for fail within
    its group. Pending checks prevent assigning that group's score and
    the total. Allowed not-applicable checks leave the denominator.
    Scores express evidence strength, not probabilities of correctness.
    """
    assessment = validate_assessment(candidate, assessment)
    metrics = []
    for group, weight in METRIC_WEIGHTS:
        checks = [
            check for check in assessment.checks
            if CHECK_DEFINITIONS[check.check_id][0] == group
        ]
        counts = {
            status: sum(check.status == status for check in checks)
            for status in ("pass", "fail", "pending", "not_applicable")
        }
        denominator = counts["pass"] + counts["fail"]
        score = None
        if not counts["pending"] and denominator:
            score = Decimal(100) * counts["pass"] / denominator
        metrics.append(MetricResult(
            group=group, weight=weight, score=score,
            passed=counts["pass"], failed=counts["fail"],
            pending=counts["pending"], not_applicable=counts["not_applicable"],
        ))

    raw = None
    if all(metric.score is not None for metric in metrics):
        raw = sum(
            (metric.score * metric.weight for metric in metrics), Decimal(0)
        ) / sum(weight for _, weight in METRIC_WEIGHTS)

    critical_pass = assessment.critical_checks_passed
    verified = raw is not None and raw >= VERIFIED_THRESHOLD and critical_pass
    final = raw
    if raw is not None and not verified:
        final = min(raw, CRITICAL_CAP)

    return ConfidenceResult(
        bundle_id=assessment.bundle_id,
        candidate_fingerprint=assessment.candidate_fingerprint,
        assessment_fingerprint=_fingerprint(assessment.model_dump(mode="json")),
        policy_fingerprint=policy_fingerprint(),
        metrics=tuple(metrics), raw_confidence=raw, confidence_score=final,
        verification_status="VERIFIED" if verified else "REVIEW_REQUIRED",
        critical_checks_passed=critical_pass, blockers=assessment.blockers,
        withheld_fields=assessment.withheld_fields,
        last_verified_at=assessment.assessed_at.isoformat() if verified else None,
    )


EVIDENCE_POLICY_VERSION = "python-evidence-confidence-v1"
INFORMATION_FIELDS = ("benefits", "eligibility", "documents", "selection_process",
                      "renewal_requirements", "application_url", "status_statements")


def _score_source_evidence_v1(candidate, assessment, *, evidence_checks_passed):
    """Measured source coverage, distinct from semantic verification confidence.

    Unknown or unproved coverage earns zero; pending semantic reviews remain
    pending. Literal eligibility earns quotation coverage, not decision approval.
    This score never overrides the existing verification gates.
    """
    assessment = validate_assessment(candidate, assessment)
    checks = {check.check_id: check.status for check in assessment.checks}
    if not evidence_checks_passed:
        raise ValueError("Source evidence must pass before confidence is assigned.")
    supported = [field for field in INFORMATION_FIELDS
                 if getattr(candidate, field).evidence_ids and
                 (getattr(candidate, field).value is not None or
                  getattr(candidate, field).missing_reason == "not_applicable")]
    auth = sum(checks[key] == "pass" for key in
               ("primary_source_ownership", "source_integrity")) * 50
    information = Decimal(100) * len(supported) / len(INFORMATION_FIELDS)
    currency = sum(checks[key] == "pass" for key in
                   ("offering_cycle_context", "availability_support", "conflict_resolution")) * Decimal(100) / 3
    value = Decimal(auth) * Decimal(".3") + information * Decimal(".5") + currency * Decimal(".2")
    verification = score_candidate(candidate, assessment)
    return {
        "policy_version": EVIDENCE_POLICY_VERSION,
        "scope": "Source authenticity, quoted field coverage and established currency; not semantic verification or probability.",
        "candidate_fingerprint": assessment.candidate_fingerprint,
        "assessment_fingerprint": verification.assessment_fingerprint,
        "confidence_score": float(value.quantize(Decimal(".01"))),
        "metrics": [{"group": group, "weight": weight, "score": float(score)}
                    for group, weight, score in (("authenticity", 30, auth),
                        ("quoted_field_coverage", 50, information), ("established_currency", 20, currency))],
        "supported_fields": supported,
        "missing_coverage_fields": [field for field in INFORMATION_FIELDS if field not in supported],
        "verification_status": verification.verification_status,
        "verification_confidence_score": verification.to_dict()["confidence_score"],
        "blockers": list(verification.blockers),
        "withheld_fields": list(verification.withheld_fields),
    }


PYTHON_CONFIDENCE_POLICY = "python-source-confidence-v7"
SOURCE_FRESHNESS_DAYS = 30


def cycle_years(value):
    """Explicit full years plus unambiguous consecutive academic-year shorthand."""
    import re
    years = set(re.findall(r"20\d{2}", value or ""))
    for first, short in re.findall(r"(20\d{2})\s*[-/]\s*(\d{2})\b", value or ""):
        last = int(first) // 100 * 100 + int(short)
        if last < int(first):
            last += 100
        if last == int(first) + 1:
            years.add(str(last))
    return years


def review_python_source(candidate, bundle_file, seed, retrieval_times, *, project_root, now=None):
    """Validate literal source facts and omissions under a known source template.

    This verifies stored-source statements, not applicant eligibility decisions,
    global document completeness, or current application availability.
    """
    from datetime import datetime, timezone
    from pathlib import Path
    from urllib.parse import urlsplit
    import re
    from decimal import Decimal
    from scholarship_intelligence.bundles import load_bundle
    from scholarship_intelligence.evidence_validation import validate_evidence
    from scholarship_intelligence.models import Claim
    root = Path(project_root).resolve()
    now = now or datetime.now(timezone.utc)
    bundle = load_bundle(bundle_file, project_root=root)
    evidence = validate_evidence(candidate, bundle_file, project_root=root)
    if not evidence.evidence_checks_passed:
        raise ValueError("Evidence must pass before source confidence is assigned.")
    source_url = urlsplit(str(candidate.official_source.value))
    template_known = seed.get("extraction_profile") in {"iit_alumni_v1", "official_sections_v1", "nsp_cards_v1", "hdfc_cards_v1"}
    from scholarship_intelligence.source_profiles import discover_scopes, scoped_blocks
    scope = seed.get("source_scope")
    if scope:
        primary_ref = next(ref for ref in bundle.documents if ref.source_url == str(candidate.official_source.value))
        structural = discover_scopes((root/primary_ref.raw_file_path).read_bytes(), seed)
        expected = next((item for item in structural if item["offering_key"] == scope["offering_key"]), None)
        if expected is None or any(expected.get(key) != scope.get(key) for key in ("card_xpath", "name", "provider", "title_xpath")):
            raise ValueError("Source card scope changed or belongs to a different programme.")
    ownership = (template_known and candidate.provider.value == (scope.get("provider") if scope else seed.get("provider_name"))
                 and source_url.scheme == "https" and source_url.hostname in seed["allowed_hosts"])
    # An independent field-to-source-section index, not another extractor run.
    labels = {
        "eligibility": r"(?:academic |financial |score and financial )?eligib(?:ility|iltiy)(?: criteri(?:a|on)| conditions| requirements| for (?:the )?scholarships?)?|financial criteria|who (?:can|is eligible to) apply(?: for (?:a |the )?chevening scholarship)?|conditions of eligibility|reservation|terms and conditions|eligible students|forfeiture of scholarship|other scholarships",
        "selection_process": r"selection(?: criteri(?:a|on)| process| procedure| of scholars| of students)?|procedure for selection|criteria of selection|methods of resolving ties|what is the procedure of selection(?: under .*)?|does reservation system is followed.*|how many slots are reserved for women candidates",
        "renewal_requirements": r"renewals?|procedure for renewal of scholarship(?: under .*)?|continuation(?: criteri(?:a|on)(?: and duration)?)?|renewal(?: requirements| criteria| of scholarship)?|duration(?: of scholarship)?|which students can apply for renewal(?: in .*)?",
        "benefits": r"benefits|scholarship benefits|(?:(?:scholarship|award) )?amount(?: and number| of scholarship)?|(?:rate|rates|quantum|value|amount) of (?:the )?scholarships?|financial assistance|what (?:does|do) (?:a |the )?chevening scholarships? cover|what is included|what is the (?:amount|rate|value) of (?:pg )?scholarship",
        "documents": r"(?:(?:required|mandatory|supporting) )?documents(?: required)?|list of mandatory documents|document checklist|what are the different documents to be uploaded with the application form",
        "cycle": r"(?:application|scholarship) cycle",
        "application_url": r"(?:how to apply|application (?:instructions|process)|procedure for applying)",
        "opening_date": r"(?:application )?(?:opening date|start date)|applications open",
        "closing_date": r"(?:application )?(?:closing date|deadline)|last date to apply",
    }
    def document_threshold(text):
        clean = re.sub(r"\s+", " ", text)
        return bool(re.search(r"scholarship\s+amount\s+(?:more than|less than|above|below|exceed)", clean, re.I)
                    and re.search(r"(?:documents?|proof).{0,120}(?:uploaded|required)|(?:upload|submit|attach).{0,140}(?:documents?|proof)", clean, re.I))
    def label(text):
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"^\s*Q\.?\s*\d+\s*[:.)]?\s*", "", text, flags=re.I)
        return re.sub(r"^\s*(?:\d+(?:\.\d+)*[.)]?|\([ivx]+\)|[ivx]+\.)\s*", "", text, flags=re.I).strip().rstrip(":.?").strip().casefold()
    passages, body = {}, []
    for ref in bundle.documents:
        active = None
        active_depth = 0
        document = json.loads((root / ref.prepared_file_path).read_bytes())
        review_blocks = scoped_blocks(document["blocks"], scope) if ref.source_url == str(candidate.official_source.value) else document["blocks"]
        for block in review_blocks:
            text = block["text"]
            chunks, pending, source_offset = [], [], 0
            for source_line in text.splitlines(keepends=True):
                stripped = source_line.strip()
                before, sep, after = source_line.partition(":")
                inline_label = sep and any(re.fullmatch(pattern, label(before)) for pattern in labels.values())
                boundary = (not stripped or any(re.fullmatch(pattern, label(stripped)) for pattern in labels.values())
                    or re.fullmatch(r"Page\s+\d+\s+of\s+\d+", stripped, re.I)
                    or re.match(r"^(?:Q\.?\s*\d+\s*[:.)]?\s+|\d+[.)]\s+(?=what|who|which|how|if|is|are|can|do|does))", stripped, re.I)
                    or re.fullmatch(r"(?:\d+(?:\.\d+)*[.)]?|\([ivx]+\))\s+[A-Z][A-Z /(),:&-]+[:.]?", stripped))
                if boundary or inline_label:
                    if pending:
                        chunks.append((pending[0][0], pending[-1][1]));pending=[]
                    if inline_label and after.strip():
                        pending.append((source_offset+len(before)+1,source_offset+len(source_line)))
                else:
                    pending.append((source_offset,source_offset+len(source_line)))
                source_offset += len(source_line)
            if pending:
                chunks.append((pending[0][0],pending[-1][1]))
            for begin, end in chunks:
                piece = text[begin:end]
                clean = re.sub(r"\s+", " ", piece)
                role = None
                monetary_prose = re.search(r"\b(?:scholarships?|financial assistance)\b.{0,100}(?:Rs\.?|INR|\u20b9)\s*\d", clean, re.I)
                income_limit = monetary_prose and re.search(r"\b(?:income|salary|earnings)\b", clean[max(0,monetary_prose.start()-100):monetary_prose.end()], re.I)
                explicit_payout = re.search(r"\b(?:scholarship\s+(?:amount|rate)(?:\s+is)?|rate\s+of\s+(?:the\s+)?scholarship|scholarships?\s+(?:is|of|worth)|financial\s+assistance\s+(?:is|of))\s*[:,\-]?\s*(?:INR|Rs\.?|\u20b9)\s*\d", clean, re.I)
                if monetary_prose and not document_threshold(clean) and (not income_limit or explicit_payout) and re.search(r"per\s+(?:month|annum|year)|monthly|annually", clean, re.I):
                    role = "benefits"
                elif not re.search(r"\bno (?:documentary |supporting )?(?:proof|documents?)", clean, re.I) and re.search(r"(?:upload|submit|enclose|provide|attach).{0,100}(?:certificate|document|proof)|(?:certificate|document|proof).{0,100}(?:uploaded|submitted|enclosed|required|attached)", clean, re.I):
                    role = "documents"
                elif re.search(r"(?:selection|slots|short.?list|tie).{0,100}(?:merit|reserved|marks|basis)|slots.{0,100}(?:reservation|women)", clean, re.I):
                    role = "selection_process"
                elif re.search(r"scholarship\s+is\s+open\s+to\s+those\s+students", clean, re.I):
                    role = "eligibility"
                if role:
                    passages.setdefault((ref.snapshot_id, block["block_id"]), []).append((role, begin, begin + end - begin))
            # Line offsets retain the exact real parent location for PDF/inline labels.
            offset = 0
            for line in text.splitlines(keepends=True) or [text]:
                pieces = [(line, offset)]
                before, sep, after = line.partition(":")
                if sep and any(re.fullmatch(pattern, label(before)) for pattern in labels.values()):
                    pieces = [(before + ":", offset), (after, offset + len(before) + 1)]
                for piece, position in pieces:
                    if not piece.strip():
                        continue
                    if re.fullmatch(r"Page\s+\d+\s+of\s+\d+", piece.strip(), re.I):
                        continue
                    normalized = label(piece)
                    matched = next((name for name, pattern in labels.items() if re.fullmatch(pattern, normalized)), None)
                    if matched:
                        active = matched
                        active_depth = len(block.get("heading_path", []))
                    elif block["kind"] == "pdf_page" and re.match(r"^(?:Q\.?\s*\d+\s*[:.)]?\s+|\d+[.)]\s+(?=what|who|which|how|if|is|are|can|do|does))", piece.strip(), re.I):
                        active = None
                    elif block["kind"] == "pdf_page" and len(piece.strip()) <= 160 and re.fullmatch(r"(?:\d+(?:\.\d+)*[.)]?|\([ivx]+\))\s+[A-Z][A-Z /(),:&-]+[:.]?", piece.strip()):
                        active = None
                    elif normalized in {"address", "phone", "email", "contact us", "donor name", "donor's name", "scholarship code", "award id", "commencement year"} or (block["kind"] == "table" and seed.get("extraction_profile") == "iit_alumni_v1"):
                        active = None
                    elif block["kind"] == "heading":
                        if active and len(block.get("heading_path", [])) > active_depth:
                            passages.setdefault((ref.snapshot_id, block["block_id"]), []).append((active, position, position + len(piece)))
                        else:
                            active = None
                    elif active and piece.strip() and not (active == "benefits" and document_threshold(piece)):
                        passages.setdefault((ref.snapshot_id, block["block_id"]), []).append((active, position, position + len(piece)))
                offset += len(line)
            body.append(text)
    text = "\n".join(body)
    quoted = {entry.evidence_id: entry for entry in candidate.evidence}
    source_blocks = {(ref.snapshot_id, block["block_id"]): block
                     for ref in bundle.documents for block in json.loads((root/ref.prepared_file_path).read_bytes())["blocks"]}
    def cited(field):
        return [quoted[key] for key in getattr(candidate, field).evidence_ids]
    def norm(value):
        return re.sub(r"\s+", " ", value).strip()
    def context_matches(field, entries):
        for entry in entries:
            valid = False
            for block_id in entry.location.block_ids:
                block = source_blocks[(entry.snapshot_id, block_id)]
                spans = sorted((begin, end) for role, begin, end in passages.get((entry.snapshot_id, block_id), []) if role == field)
                ranges = []
                for begin, end in spans:
                    if ranges and (begin <= ranges[-1][1] or not block["text"][ranges[-1][1]:begin].strip()):
                        ranges[-1] = (ranges[-1][0], max(end, ranges[-1][1]))
                    else:
                        ranges.append((begin, end))
                offset = 0
                while True:
                    start = block["text"].find(entry.quote, offset)
                    if start < 0:
                        break
                    if any(begin <= start and start + len(entry.quote) <= end for begin, end in ranges):
                        valid = True
                        break
                    offset = start + 1
                # Real HTML headings can also supply context independently.
                if any(re.fullmatch(labels.get(field, r"(?!)"), label(heading)) for heading in block.get("heading_path", [])):
                    valid = True
            if not valid:
                return False
        return bool(entries)
    def date_supported(value, field, entries):
        from datetime import datetime, date
        date_pattern = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[-/]\d{1,2}[-/]\d{4}|\d{1,2}(?:st|nd|rd|th)?[ -]+[A-Za-z]+[ ,.-]+\d{4}|[A-Za-z]+[ -]+\d{1,2}(?:st|nd|rd|th)?[ ,]+\d{4}", re.I)
        roles = re.compile(r"(?P<other>(?:institute|institution|defective|nodal|DNO|SNO|MNO).*?verification.*?(?:till|until|deadline))|(?P<close>student application open till|closing date|closure date|last date(?=\s*:)|last date (?:to apply|of application|for submission)|application deadline|deadline for applications?|applications? (?:are )?(?:open (?:until|till)|close[sd]?)|and closes?)|(?P<open>scheme open from|opening date|start date|applications? (?:are )?(?:open|start)[sd]?)", re.I)
        for entry in entries:
            for token in date_pattern.finditer(entry.quote):
                raw = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", token[0], flags=re.I).replace("Sept", "Sep")
                parsed = None
                try:
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                        parsed = date.fromisoformat(raw)
                    elif re.fullmatch(r"\d{1,2}[-/]\d{1,2}[-/]\d{4}", raw):
                        first, second, year = map(int, re.split(r"[-/]", raw))
                        if first > 12 or first == second or seed.get("date_order") == "DMY":
                            parsed = date(year, second, first)
                        elif second > 12 or seed.get("date_order") == "MDY":
                            parsed = date(year, first, second)
                    else:
                        normalized = re.sub(r"[ ,.-]+", " ", raw).strip()
                        for fmt in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
                            try:
                                parsed = datetime.strptime(normalized, fmt).date()
                                break
                            except ValueError:
                                pass
                except ValueError:
                    pass
                preceding = list(roles.finditer(entry.quote[:token.start()]))
                role = preceding[-1].lastgroup if preceding else None
                if role is None and len(list(date_pattern.finditer(entry.quote))) == 1:
                    labels_here = list(roles.finditer(entry.quote))
                    role = labels_here[-1].lastgroup if labels_here else None
                    if role is None:
                        keys = [key for key in source_blocks if key[0] == entry.snapshot_id]
                        for block_id in entry.location.block_ids:
                            block_key = (entry.snapshot_id, block_id)
                            position = keys.index(block_key)
                            contexts = source_blocks[block_key].get("heading_path", []) + ([source_blocks[keys[position - 1]]["text"]] if position else [])
                            for context in contexts:
                                if re.fullmatch(r"(?:application )?(?:opening date|start date|closing date|deadline|last date(?: to apply| of application)?|scheme open from|student application open till)", label(context)):
                                    role = "open" if re.search(r"opening|start|scheme open from", context, re.I) else "close"
                if parsed == value.calendar_date and role == ("open" if field == "opening_date" else "close"):
                    if re.search(r"renewal|date of birth|payment|disbursement", entry.quote, re.I):
                        continue
                    if re.search(r"\b(?:\d{1,2}:\d{2}|AM|PM|IST|UTC|GMT)\b", entry.quote, re.I) and not value.time_qualifier:
                        continue
                    return value.stage == "student_application"
        return False
    fields = {}
    cue_patterns = {"documents": r"document|certificate|affidavit|income proof", "benefits": r"INR|Rs\.?|rupees|tuition",
                    "eligibility": r"eligib|criteri|must|shall", "selection_process": r"selection|tie.break|shortlist",
                    "renewal_requirements": r"renewal|continuation|duration", "application_url": r"apply now|application form|submit application",
                    "status_statements": r"applications? (?:(?:are |now |currently )open|closed)|no application (?:needed|required)",
                    "opening_date": r"opening date|applications? (?:are )?open|scheme open from",
                    "closing_date": r"deadline|closing date|last date|student application open till",
                    "cycle": r"application cycle|scholarship cycle"}
    for name in type(candidate).model_fields:
        claim = getattr(candidate, name)
        if not isinstance(claim, Claim):
            continue
        entries = cited(name)
        value = claim.value
        status, reason = "unresolved", "Source support or context has not been established."
        if claim.missing_reason == "ambiguous_or_conflicting":
            reason = claim.explanation
        elif value is None:
            if claim.missing_reason == "not_applicable" and any(re.fullmatch(r"no application (?:needed|required)[.!]?", entry.quote.strip(), re.I) for entry in entries):
                status, reason = "not_applicable", "Explicit no-application statement; this does not establish a current award cycle."
            elif name not in {"scholarship_name", "provider", "official_source"} and template_known and not re.search(cue_patterns.get(name, r"(?s)."), text, re.I):
                status, reason = "not_specified", "No relevant field cue found in inspected documents; provider-wide absence is not established."
            else:
                reason = "Required value missing, relevant source cues remain unparsed, or this template needs review."
        else:
            valid = False
            if name == "scholarship_name":
                valid = any(value == entry.quote and any(source_blocks[(entry.snapshot_id, key)]["kind"] == "heading" or (scope and source_blocks[(entry.snapshot_id,key)]["text"] == scope["name"]) for key in entry.location.block_ids) for entry in entries)
            elif name == "provider":
                valid = ownership and any(value.casefold() in entry.quote.casefold() for entry in entries)
            elif name == "official_source":
                valid = any(str(value) == ref.source_url and ref.snapshot_id in [entry.snapshot_id for entry in entries] for ref in bundle.documents)
            elif name == "application_url":
                valid = any(str(value) == link.get("url") and re.search(r"apply|application", link.get("text", ""), re.I)
                            for entry in entries for key in entry.location.block_ids for link in source_blocks[(entry.snapshot_id, key)].get("links", []))
            elif name in {"opening_date", "closing_date"}:
                valid = date_supported(value, name, entries)
            elif name == "status_statements":
                valid = all(any(item.statement == entry.quote for entry in entries) for item in value)
            elif name == "cycle":
                valid = any(str(value) in entry.quote and re.search(r"academic year|scholarship|programme|program|cycle",entry.quote,re.I) for entry in entries)
            elif context_matches(name, entries):
                original = norm("\n".join(entry.quote for entry in entries))
                complete = True
                required_passages_found = False
                for key, spans in passages.items():
                    ranges = sorted((begin, end) for role, begin, end in spans if role == name)
                    merged = []
                    for begin, end in ranges:
                        if merged and begin <= merged[-1][1]:
                            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                        else:
                            merged.append((begin, end))
                    if not merged:
                        continue
                    required_passages_found = True
                    parent = source_blocks[key]["text"]
                    quotes = [entry.quote for entry in entries if entry.snapshot_id == key[0] and key[1] in entry.location.block_ids]
                    covered = set()
                    for quote in quotes:
                        start = 0
                        while True:
                            offset = parent.find(quote, start)
                            if offset < 0:
                                break
                            covered.update(range(offset, offset + len(quote)))
                            start = offset + 1
                    # Every non-whitespace source character must be cited in
                    # this same block. Citation order and duplicates are irrelevant.
                    if any(index not in covered and not parent[index].isspace()
                           for begin, end in merged for index in range(begin, end)):
                        complete = False
                complete = complete and required_passages_found
                if name == "selection_process":
                    valid = norm(value) == original
                elif name == "eligibility":
                    valid = norm(value.summary) == original
                    node = value.conditions
                    if node.kind != "condition" or norm(node.statement) != original or node.numeric is not None:
                        valid = False
                        reason = "Structured eligibility logic needs independently assessed semantic/numeric support."
                elif name == "renewal_requirements":
                    valid = norm(value.summary) == original and all(any(condition == entry.quote for entry in entries) for condition in value.conditions)
                elif name == "documents":
                    valid = all(any(item.name == entry.quote and (item.condition is None or
                        (item.condition == "Institution verification" and re.search(r"institution(?:s)?.{0,100}(?:upload|verify)", norm(entry.quote), re.I))) for entry in entries) for item in value)
                elif name == "cycle":
                    valid = any(value == entry.quote.strip() for entry in entries)
                elif name == "benefits":
                    valid = all(any(item.description in entry.quote for entry in entries) for item in value)
                    for item in value:
                        if item.minimum_amount is not None or item.maximum_amount is not None or item.amount_kind not in {"fixed", "maximum", "minimum", "unspecified"}:
                            valid = False
                            reason = "Benefit range or qualifier needs independent numeric verification."
                        amounts = re.findall(r"(?:INR|Rs\.?|₹)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(lakh(?:s)?|lac(?:s)?|crore(?:s)?|thousand)?", item.description, re.I)
                        if item.amount is not None:
                            normalized = [Decimal(digits.replace(",", "")) * (100000 if scale.lower().startswith(("lakh", "lac")) else 10000000 if scale.lower().startswith("crore") else 1000 if scale else 1) for digits, scale in amounts]
                            valid = valid and item.currency == "INR" and len(normalized) == 1 and normalized[0] == item.amount
                            amount_prefix = re.split(r"(?:INR|Rs\.?|\u20b9)\s*[0-9]", item.description, maxsplit=1, flags=re.I)[0][-50:]
                            if item.amount_kind == "maximum" and not re.search(r"up to|upto|maximum", amount_prefix, re.I):
                                valid = False
                            if item.amount_kind == "minimum" and not re.search(r"minimum|at least", amount_prefix, re.I):
                                valid = False
                            if item.amount_kind == "fixed" and re.search(r"up to|upto|maximum|minimum|at least|range|depending", amount_prefix, re.I):
                                valid = False
                        elif amounts:
                            valid = False
                            reason = "Source states money but the amount is not normalized; literal description alone is incomplete extraction."
                        if re.search(r"(?:£|\$|€|GBP|USD|EUR)\s*\d", item.description) and item.amount is None:
                            valid = False
                            reason = "A foreign-currency amount is stated but is not normalized; retain its literal quote for review."
                        expected_period = ("monthly" if re.search(r"\b(?:monthly|per\s+month|p\.?m\.?)\b", item.description, re.I) else
                                           "annual" if re.search(r"\b(?:annually|annual|per\s+annum|per\s+year|p\.?a\.?)\b", item.description, re.I) else
                                           "one_time" if re.search(r"\bone[- ]time\b", item.description, re.I) else "whole_course" if re.search(r"duration of (?:the |their )?(?:course|programme)|whole course", item.description, re.I) else "unspecified")
                        if item.period != expected_period:
                            valid = False
                            reason = "Benefit period is not normalized consistently with its source wording."
                if name in {"eligibility", "selection_process", "renewal_requirements", "benefits", "documents"} and not complete:
                    valid = False
                    reason = "Source section contains material not represented by this field's cited text; completeness review required."
            if valid and template_known:
                status, reason = "supported", "Independent value/citation/field-context checks passed; no second extractor run was used."
            elif reason.startswith("Source support"):
                status, reason = "unsupported", "Value or its field context is not independently supported."
        fields[name] = {"status": status, "reason": reason, "evidence_ids": claim.evidence_ids,
                        "inspected_snapshot_ids": candidate.snapshot_ids}
    static = {"review_version": "python-source-review-v5", "candidate_fingerprint": candidate_fingerprint(candidate),
              "official_ownership_passed": ownership, "evidence_checks_passed": True,
              "evidence_validator_version": evidence.validator_version,
              "fields": fields, "retrieval_times": dict(retrieval_times), "eligibility_decisions_supported": False,
              "scope": "Independently supported core fields and programme-specific dated context. Quotation accuracy, completeness and availability are separate; automated eligibility decisions remain unsupported."}
    static["context_blockers"] = []
    canonical = re.sub(r"\s+", " ", text)
    if (re.search(r"student who gets another scholarship can apply.{0,100}Reply:\s*No", canonical, re.I)
            and re.search(r"(?:student|applicants).{0,100}already in receipt.{0,160}eligible to apply.{0,120}declared", canonical, re.I)):
        static["context_blockers"].append("eligibility_conflict: The FAQ denies applying while receiving another scholarship, but the guidelines describe declaring an existing benefit and relinquishing it after selection. Authoritative clarification is required.")
    tie_clause = re.search(r"In case of tie.{0,1400}", canonical, re.I)
    if tie_clause and len(re.findall(r"basis.{0,30}marks.{0,70}(?:Under-graduation|\(UG\))", tie_clause[0], re.I)) > 1:
        static["context_blockers"].append("selection_tie_break: Successive tie-break clauses repeat undergraduate marks without establishing distinct criteria; preserve both clauses for review.")
    if scope:
        expected_urls = set(expected.get("support_urls", []))
        missing = expected_urls - {ref.source_url for ref in bundle.documents}
        if missing:
            static["context_blockers"].append("source_completeness: Linked programme guidance is unavailable: " + ", ".join(sorted(missing)))
        if scope.get("shared_support_urls"):
            static["context_blockers"].append("programme_association: Shared multi-programme guidance needs a programme-specific section review; no scope was inferred from a generic PDF.")
        if scope.get("catalogue_conflicts"):
            static["context_blockers"].append("catalogue_conflict: Different catalogue responses disagree about this programme; resolve its source scope before verification.")
    if seed.get("seed_id") == "ffe" and re.search(r"exclusively.*JEE Main", text, re.I|re.S) and re.search("JEE Advanced",text,re.I):
        static["fields"]["eligibility"].update(status="unresolved",reason="Conflicting IIT JEE Main/Advanced wording requires authoritative clarification.")
    return redate_source_review(candidate, static, now=now)



def redate_source_review(candidate, review, *, now=None):
    """Refresh time-dependent state after source fields have been rechecked."""
    import re
    from datetime import datetime, timezone, timedelta
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Scoring time must include a timezone.")
    retrieval_times = dict(review["retrieval_times"])
    fields = review["fields"]
    ownership = review["official_ownership_passed"]
    ages, freshness_issues = [], []
    for snapshot_id in candidate.snapshot_ids:
        value = retrieval_times.get(snapshot_id)
        try:
            retrieved = datetime.fromisoformat(value)
            if retrieved.tzinfo is None or retrieved.utcoffset() is None:
                raise ValueError("Timezone missing")
            age = now - retrieved
            if age < -timedelta(minutes=5):
                raise ValueError("Retrieval time is in the future")
            ages.append(max(0, age.total_seconds() / 86400))
        except (ValueError, TypeError):
            freshness_issues.append("Missing or invalid retrieval timestamp for " + snapshot_id)
    fresh = bool(ages) and not freshness_issues and max(ages) <= SOURCE_FRESHNESS_DAYS
    freshness = "FRESH" if fresh else "UNKNOWN" if freshness_issues else "STALE"
    blockers = [name + ": " + item["reason"] for name, item in fields.items() if item["status"] in {"unsupported", "unresolved"}]
    if not ownership:
        blockers.append("Official-source ownership requires a supported provider profile.")
    blockers.extend(freshness_issues)
    blockers.extend(review.get("context_blockers", []))
    if freshness == "STALE":
        blockers.append("Saved source retrieval exceeds the 30-day freshness policy; re-fetch before current verification.")
    # Lifecycle depends only on supported student-window evidence, not the
    # overall factual score. Qualified/route-specific windows stay unresolved.
    opening, closing = candidate.opening_date.value, candidate.closing_date.value
    lifecycle, lifecycle_reason = "REVIEW_REQUIRED", "No complete supported student application window; missing dates do not imply closure."
    today = now.astimezone(timezone(timedelta(hours=5, minutes=30))).date()
    if closing is not None and fields["closing_date"]["status"] == "supported" and closing.stage == "student_application" and not closing.route and not closing.time_qualifier:
        end = closing.calendar_date
        if opening is not None and (opening.stage != "student_application" or opening.route or opening.time_qualifier or opening.calendar_date > end):
            lifecycle_reason = "Opening and closing window contexts are incompatible or qualified."
        elif end < today:
            lifecycle, lifecycle_reason = "EXPIRED", "Supported unqualified student closing date precedes today."
        elif fresh and opening is not None and fields["opening_date"]["status"] == "supported" and opening.calendar_date <= today < end:
            lifecycle, lifecycle_reason = "ACTIVE", "Today is within the supported unqualified student application window."
        else:
            lifecycle_reason = "Opening date or an unambiguous cutoff is not established for today."
    if fresh and fields["status_statements"]["status"] == "supported" and candidate.status_statements.value:
        states = {item.statement.strip().rstrip(".! ").casefold() for item in candidate.status_statements.value}
        if states and all(re.fullmatch(r"applications? (?:are )?closed", state) for state in states):
            lifecycle, lifecycle_reason = "EXPIRED", "The current official programme page explicitly states applications are closed. No closing date was invented."
    return {**review, "assessed_at": now.isoformat(), "source_freshness": freshness,
            "maximum_source_age_days": round(max(ages), 3) if ages else None,
            "blockers": blockers, "lifecycle_status": lifecycle, "lifecycle_reason": lifecycle_reason}


def score_source_evidence(candidate, assessment, *, evidence_checks_passed, source_review=None):
    """One current Python factual-confidence policy; v1 retained for old artifacts."""
    assessment = validate_assessment(candidate, assessment)
    if source_review is None:
        return _score_source_evidence_v1(candidate, assessment, evidence_checks_passed=evidence_checks_passed)
    if not evidence_checks_passed or source_review.get("candidate_fingerprint") != candidate_fingerprint(candidate):
        raise ValueError("Source review must match a passing candidate.")
    fields = source_review["fields"]
    applicable = [item for item in fields.values() if item["status"] != "not_specified"]
    supported = sum(item["status"] in {"supported", "not_applicable"} for item in applicable)
    auth = 100 if source_review["official_ownership_passed"] else 50
    support = Decimal(100) * supported / len(applicable) if applicable else Decimal(0)
    opening, closing = candidate.opening_date.value, candidate.closing_date.value
    applicable_window = (opening is not None and closing is not None
                         and fields["opening_date"]["status"] == fields["closing_date"]["status"] == "supported"
                         and opening.calendar_date <= closing.calendar_date
                         and not any(value.route or value.time_qualifier for value in (opening, closing)))
    no_application = candidate.application_url.value is None and fields["application_url"]["status"] == "not_applicable"
    cycle = candidate.cycle.value
    import re
    from datetime import datetime
    cycle_current = (cycle is not None and fields["cycle"]["status"] == "supported"
                     and str(datetime.fromisoformat(source_review["assessed_at"]).year) in cycle_years(cycle))
    closing_supported = closing is not None and fields["closing_date"]["status"] == "supported" and not closing.route and not closing.time_qualifier
    context_established = applicable_window or (cycle_current and (no_application or closing_supported))
    currentness = 100 if context_established and source_review["source_freshness"] == "FRESH" else 0
    value = Decimal(auth) * Decimal(".3") + support * Decimal(".5") + Decimal(currentness) * Decimal(".2")
    blockers = list(source_review["blockers"])
    mandatory = ("scholarship_name", "provider", "official_source", "benefits", "eligibility")
    for name in mandatory:
        if fields[name]["status"] != "supported" and not any(item.startswith(name + ":") for item in blockers):
            blockers.append(name + ": A core scholarship field is not independently verified.")
    if fields["application_url"]["status"] not in {"supported", "not_applicable"}:
        blockers.append("application_route: The source-supported application route has not been established.")
    if no_application and applicable_window:
        blockers.append("application_route: Source says no application is needed but also supplies a student application window; resolve the route conflict.")
    if not context_established:
        blockers.append("offering_context: No verified programme-specific application window or current explicitly dated automatic-award cycle; retrieval recency is insufficient.")
    critical = not blockers and currentness == 100 and all(fields[name]["status"] == "supported" for name in mandatory)
    verified = critical and value >= VERIFIED_THRESHOLD
    if not critical:
        value = min(value, CRITICAL_CAP)
    return {"policy_version": PYTHON_CONFIDENCE_POLICY, "scope": source_review["scope"],
            "candidate_fingerprint": source_review["candidate_fingerprint"],
            "source_authenticity_status": "CONFIRMED" if source_review["official_ownership_passed"] else "REVIEW_REQUIRED",
            "confidence_score": float(value.quantize(Decimal(".01"))),
            "verification_confidence_score": float(value.quantize(Decimal(".01"))),
            "verification_status": "VERIFIED" if verified else "REVIEW_REQUIRED",
            "critical_checks_passed": critical,
            "metrics": [{"group": group, "weight": weight, "score": float(score)} for group, weight, score in
                        (("authenticity", 30, auth), ("factual_support", 50, support), ("offering_context", 20, currentness))],
            "supported_fields": [name for name, item in fields.items() if item["status"] in {"supported", "not_applicable"}],
            "missing_coverage_fields": [name for name, item in fields.items() if item["status"] == "not_specified"],
            "withheld_fields": [name for name, item in fields.items() if item["status"] in {"unsupported", "unresolved"}],
            "field_assessments": fields, "blockers": blockers,
            "source_freshness": source_review["source_freshness"],
            "source_age_days": source_review["maximum_source_age_days"],
            "eligibility_decisions_supported": False,
            "coverage": {"present": sum(getattr(candidate, name).value is not None for name in fields),
                         "not_specified": sum(item["status"] == "not_specified" for item in fields.values()),
                         "unresolved": sum(item["status"] in {"unsupported", "unresolved"} for item in fields.values())}}
