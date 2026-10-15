"""Local Streamlit scholarship dashboard with explicit background crawl controls.

Record browsing is read-only. The daily scheduler and Crawl now button run the
shared live discovery, extraction and verification workflow.
"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from scholarship_intelligence.settings import DATABASE_PATH, PROJECT_ROOT
from scholarship_intelligence.models import classify_offering
from scholarship_intelligence.bundles import document_checks


IST = timezone(timedelta(hours=5, minutes=30), "IST")


def _decode(value, fallback):
    return fallback if value is None else json.loads(value)


@document_checks()
def load_records(database_path=DATABASE_PATH, *, project_root=PROJECT_ROOT, include_source_assessments=True):
    """Read facts, evidence and history in one consistent SQLite snapshot."""
    database = Path(database_path).resolve()
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        records = [dict(row) for row in connection.execute(
            "SELECT s.*, p.name, p.provider_name FROM scholarships s "
            "JOIN programmes p ON p.programme_id=s.programme_id ORDER BY s.scholarship_id"
        )]
        sources = [dict(row) for row in connection.execute(
            "SELECT ss.scholarship_id, ss.role, src.* FROM scholarship_sources ss "
            "JOIN sources src ON src.source_id=ss.source_id ORDER BY ss.scholarship_id,src.source_id"
        )]
        evidence = [dict(row) for row in connection.execute(
            "SELECT ev.*, snap.retrieved_at, snap.content_hash, snap.raw_file_path, "
            "snap.prepared_content_path, src.url AS source_url FROM evidence ev "
            "JOIN source_snapshots snap ON snap.snapshot_id=ev.snapshot_id "
            "JOIN sources src ON src.source_id=snap.source_id ORDER BY ev.evidence_id"
        )]
        changes = [dict(row) for row in connection.execute(
            "SELECT * FROM changes ORDER BY detected_at DESC, change_id DESC"
        )]
        by_id = {record["scholarship_id"]: record for record in records}
        for record in records:
            record["details"] = _decode(record["verification_details_json"], {})
            claims = {**record["details"].get("accepted_claims", {}), **record["details"].get("draft_claims", {})}
            record["offering_classification"] = classify_offering(record["name"], claims)
            record["offering_type"] = record["offering_classification"]["offering_type"]
            record["sources"], record["evidence"], record["changes"] = [], [], []
        for row in sources:
            row["authenticity"] = _decode(row["authenticity_json"], {})
            by_id[row["scholarship_id"]]["sources"].append(row)
        for row in evidence:
            row["location"] = _decode(row["location_json"], {})
            row["check"] = _decode(row["check_result_json"], {})
            by_id[row["scholarship_id"]]["evidence"].append(row)
        for row in changes:
            by_id[row["scholarship_id"]]["changes"].append(row)
    finally:
        connection.close()
    # All database rows came from one snapshot. Release its read lock before
    # checking immutable source files so background crawls can commit progress.
    source_context = {}
    if include_source_assessments:
        for record in records:
            apply_source_assessment(record, project_root=project_root, context_cache=source_context)
            apply_availability_context(record, project_root=project_root)
        cache_path = Path(project_root) / 'data/runtime/http-cache.json'
        checks = json.loads(cache_path.read_bytes()) if cache_path.exists() else {}
        for record in records:
            urls = {item['url'] for item in record['sources']}
            failed = [item for item in checks.values() if item.get('status') == 'failed'
                      and (item.get('snapshot', {}).get('source_url') or item.get('url')) in urls]
            if failed:
                record['latest_source_check'] = 'A source could not be rechecked during the latest live update.'
                record['verification_status'] = 'REVIEW_REQUIRED'
                record['lifecycle_status'] = 'NO_LONGER_VERIFIABLE'
                record['confidence_score'] = None
    return records



def apply_source_assessment(record, *, project_root=PROJECT_ROOT, context_cache=None):
    """Read a pinned source draft; recompute scores rather than trust stored numbers."""
    packet = record["details"].get("python_source_assessment")
    if not packet:
        return
    try:
        from scholarship_intelligence.models import ScholarshipExtraction
        from scholarship_intelligence.semantic_assessment import SemanticAssessment
        from scholarship_intelligence.evidence_validation import validate_evidence
        from scholarship_intelligence.confidence import score_source_evidence, review_python_source, redate_source_review, PYTHON_CONFIDENCE_POLICY
        from scholarship_intelligence.scholarship_storage import reviewed_lifecycle
        root = Path(project_root).resolve()
        def pinned(path, digest):
            file = (root / path).resolve()
            if not file.is_relative_to(root / "data/runtime/extractions"):
                raise ValueError("Assessment artifact outside extraction storage.")
            content = file.read_bytes()
            if sha256(content).hexdigest() != digest:
                raise ValueError("Assessment artifact changed.")
            return content
        candidate = ScholarshipExtraction.model_validate_json(pinned(packet["candidate_path"], packet["candidate_sha256"]))
        origin = json.loads(pinned(packet["provenance_path"], packet["provenance_sha256"]))
        from scholarship_intelligence.semantic_assessment import candidate_fingerprint
        if origin.get("candidate_fingerprint") != candidate_fingerprint(candidate) or candidate.bundle_id != packet["bundle_id"]:
            raise ValueError("Assessment provenance mismatch.")
        source = str(candidate.official_source.value)
        accepted_source = record["details"].get("accepted_claims", {}).get("official_source", {}).get("value")
        from scholarship_intelligence.source_profiles import programme_key
        scope = packet.get("source_scope")
        if scope and programme_key(candidate.scholarship_name.value) != programme_key(record["name"]):
            raise ValueError("Assessment belongs to a different programme card.")
        if source != accepted_source and not (scope and set(candidate.snapshot_ids) and
                any(item["url"] in {source, *scope.get("support_urls", [])} for item in record["sources"])):
            raise ValueError("Assessment does not belong to this stored source.")
        evidence = validate_evidence(candidate, root / "data/runtime/bundles" / (candidate.bundle_id + ".json"), project_root=root)
        assessment = SemanticAssessment.model_validate(packet["assessment"])
        if packet["score"]["policy_version"] in {PYTHON_CONFIDENCE_POLICY, "python-source-confidence-v6"}:
            cache = context_cache if context_cache is not None else {}
            pilot_path = (root / packet["pilot_file"]).resolve()
            if not pilot_path.is_relative_to(root / "data/runtime"):
                raise ValueError("Retrieval report outside runtime storage.")
            if pilot_path not in cache:
                content = pilot_path.read_bytes()
                cache[pilot_path] = (sha256(content).hexdigest(), [json.loads(line) for line in content.decode("utf-8").splitlines() if line.strip()])
            digest, events = cache[pilot_path]
            if digest != packet["pilot_sha256"]:
                raise ValueError("Retrieval report changed.")
            if "seeds" not in cache:
                cache["seeds"] = json.loads((root / "config/seeds.json").read_bytes())["seeds"]
            seed = next(item for item in cache["seeds"] if item["seed_id"] == packet["seed_id"])
            from scholarship_intelligence.bundles import load_bundle
            bundle_file = root / "data/runtime/bundles" / (candidate.bundle_id + ".json")
            times = {}
            for ref in load_bundle(bundle_file, project_root=root).documents:
                matches = [event["snapshot"]["retrieved_at"] for event in events if event.get("event") == "fetched"
                           and event.get("http_status") == 200 and event.get("seed_id") == seed["seed_id"]
                           and event["snapshot"]["source_url"] == ref.source_url and event["snapshot"]["content_hash"] == ref.content_hash]
                if matches:
                    times[ref.snapshot_id] = max(matches, key=datetime.fromisoformat)
            try:
                from scholarship_intelligence.reuse import checked
                checked(packet, seed, root)
                current_review = redate_source_review(candidate, packet['source_review'])
            except (OSError, ValueError, KeyError, TypeError):
                current_review = review_python_source(candidate, bundle_file, {**seed, "source_scope": scope}, times, project_root=root)
            saved_review = redate_source_review(candidate, current_review, now=datetime.fromisoformat(packet["source_review"]["assessed_at"]))
            saved_score = score_source_evidence(candidate, assessment, evidence_checks_passed=evidence.evidence_checks_passed, source_review=saved_review)
            if packet["score"]["policy_version"] == PYTHON_CONFIDENCE_POLICY and (saved_review != packet["source_review"] or saved_score != packet["score"]):
                raise ValueError("Saved source review or confidence was altered.")
            score = score_source_evidence(candidate, assessment, evidence_checks_passed=evidence.evidence_checks_passed, source_review=current_review)
            record["source_review"] = current_review
            if saved_score["verification_status"] == "VERIFIED":
                record["last_verified_at"] = packet.get("source_last_verified_at", packet["source_review"]["assessed_at"])
        else:
            if packet["score"]["policy_version"] in {"python-source-confidence-v2", "python-source-confidence-v3", "python-source-confidence-v4", "python-source-confidence-v5"}:
                raise ValueError("Superseded confidence policy; run score-seed again.")
            score = score_source_evidence(candidate, assessment, evidence_checks_passed=evidence.evidence_checks_passed)
            if score != packet["score"]:
                raise ValueError("Stored assessment score does not match the policy.")
        record["availability_verified"] = record.get("source_review", {}).get("source_freshness") == "FRESH" and record.get("source_review", {}).get("lifecycle_status") in {"ACTIVE", "EXPIRING_SOON", "EXPIRED"}
        record["source_candidate"] = candidate.model_dump(mode="json")
        record["source_score"] = score
        record["confidence_score"] = score["confidence_score"]
        record["verification_status"] = score["verification_status"]
        record["application_mode"] = ("No student application required" if score.get("field_assessments", {}).get("application_url", {}).get("status") == "not_applicable" else "Student application route" if score.get("field_assessments", {}).get("application_url", {}).get("status") == "supported" else "Not established")
        record["lifecycle_status"] = record["source_review"]["lifecycle_status"] if "source_review" in record else reviewed_lifecycle(record["source_candidate"], score["verification_status"])
        for field in ("opening_date", "closing_date"):
            value = record["source_candidate"][field]["value"]
            record[field] = value["calendar_date"] if value else None
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        record["source_assessment_error"] = "The scored source draft changed or failed validation. Run score-seed again after rechecking sources."
        record["confidence_score"] = None
        record["verification_status"] = record["lifecycle_status"] = "REVIEW_REQUIRED"


def apply_availability_context(record, *, project_root=PROJECT_ROOT):
    """Recheck programme association and exact deadline evidence on every read."""
    packet = record["details"].get("application_context")
    if not packet:
        return
    try:
        from scholarship_intelligence.pipeline import nsp_application_context
        from scholarship_intelligence.bundles import load_bundle
        root = Path(project_root).resolve()
        manifest = load_bundle(root / "data/runtime/bundles" / (packet["bundle_id"] + ".json"), project_root=root)
        ref = manifest.documents[0]
        if ref.source_url != "https://scholarships.gov.in/All-Scholarships":
            raise ValueError("Unexpected availability source.")
        receipt_path = (root / packet["receipt_path"]).resolve()
        if not receipt_path.is_relative_to(root / "data/runtime/diagnostics"):
            raise ValueError("Retrieval receipt outside runtime diagnostics.")
        receipt_bytes = receipt_path.read_bytes()
        if sha256(receipt_bytes).hexdigest() != packet["receipt_sha256"]:
            raise ValueError("Availability receipt changed.")
        receipt = json.loads(receipt_bytes)
        if receipt["source_url"] != ref.source_url or receipt["content_hash"] != ref.content_hash:
            raise ValueError("Availability receipt does not belong to this source.")
        current = nsp_application_context(record, (root / ref.raw_file_path).read_bytes(),
                                          receipt["retrieved_at"])
        if current != {key: value for key, value in packet.items() if key not in {"bundle_id", "receipt_path", "receipt_sha256"}}:
            raise ValueError("Availability evidence or programme association changed.")
        retrieved = datetime.fromisoformat(current["retrieved_at"])
        fresh = timedelta(0) <= datetime.now(timezone.utc) - retrieved <= timedelta(days=30)
        record["availability_verified"] = fresh
        record["application_mode"] = "Student application window"
        record["opening_date"] = current["opening_date"]
        record["closing_date"] = current["closing_date"]
        today = datetime.now(IST).date().isoformat()
        record["lifecycle_status"] = ("EXPIRED" if today > current["closing_date"] else
                                      "ACTIVE" if fresh and current["opening_date"] <= today < current["closing_date"] else "REVIEW_REQUIRED")
        record["application_context"] = current
    except (OSError, ValueError, KeyError, TypeError):
        record["availability_verified"] = False
        record["availability_error"] = "Application evidence changed or no longer matches this programme; refresh availability."
        record["lifecycle_status"] = "REVIEW_REQUIRED"


def _latest_source(st, record):
    if record.get("source_assessment_error"):
        st.error(record["source_assessment_error"])
        return
    candidate = record.get("source_candidate")
    if not candidate:
        st.info("No seed evidence assessment is saved for this offering.")
        return
    score = record["source_score"]
    st.caption(score["scope"])
    _table(st, [{"Metric": _label(item["group"]), "Weight": str(item["weight"]) + "%", "Score": round(item["score"], 2)} for item in score["metrics"]])
    coverage = score.get("coverage", {})
    if coverage:
        st.caption(f"Fields present: {coverage['present']} · Not specified in inspected documents: {coverage['not_specified']} · Unresolved: {coverage['unresolved']}")
    source_url = _web_url(candidate["official_source"]["value"])
    if source_url:
        st.link_button("Open official source", source_url)
    if record.get("source_review"):
        st.caption("Application status: " + record["source_review"]["lifecycle_reason"])
    for blocker in score["blockers"]:
        st.warning(blocker)
    evidence = {item["evidence_id"]: item for item in candidate["evidence"]}
    for field, claim in candidate.items():
        if not isinstance(claim, dict) or "value" not in claim:
            continue
        with st.expander(_label(field) + (" — withheld" if field in score["withheld_fields"] else "")):
            review = score.get("field_assessments", {}).get(field, {})
            st.caption(_label(review.get("status", "unreviewed")) + ": " + review.get("reason", ""))
            display_claim = dict(claim)
            if claim["value"] is None and review.get("status") == "not_specified":
                display_claim.update(missing_reason="not_specified_in_inspected_material", explanation=review["reason"])
            labels = {"documents": "Required documents", "renewal_requirements": "Renewal and continuation", "status_statements": "Source status statements"}
            if field == "eligibility" and claim["value"] is not None:
                st.text(claim["value"]["summary"])
            else:
                _claim(st, labels.get(field, _label(field)), display_claim)
            for identity in claim["evidence_ids"]:
                quote = evidence[identity]
                st.text(quote["quote"])
                st.caption("Evidence: " + identity)
                st.json(quote["location"], expanded=False)


def filter_records(records, search="", provider="All", category="All",
                   verification="All", lifecycle="All", *, scholarships_only=True):
    query = search.strip().casefold()
    return [record for record in records if
            (not scholarships_only or classify_offering(record.get("name"),
                {**record.get("details", {}).get("accepted_claims", {}), **record.get("details", {}).get("draft_claims", {})})["main_dashboard_eligible"])
            and (not query or query in (record["name"] + " " + record["provider_name"]).casefold())
            and (provider == "All" or record["provider_name"] == provider)
            and (category == "All" or any(source["source_type"] == category for source in record["sources"]))
            and (verification == "All" or record["verification_status"] == verification)
            and (lifecycle == "All" or record["lifecycle_status"] == lifecycle)]


def summarize(records, now=None):
    now = now or datetime.now(timezone.utc)
    assigned = [record["confidence_score"] for record in records if record["confidence_score"] is not None]
    recent = 0
    for record in records:
        value = record["last_changed_at"]
        if value:
            when = datetime.fromisoformat(value)
            if when.tzinfo is not None and timedelta(0) <= now - when <= timedelta(days=7):
                recent += 1
    return {
        "stored": len(records),
        "verified": sum(record["verification_status"] == "VERIFIED" for record in records),
        "review_required": sum(record["verification_status"] == "REVIEW_REQUIRED" for record in records),
        "active": sum(record["lifecycle_status"] in {"ACTIVE", "EXPIRING_SOON"} for record in records),
        "expired": sum(record["lifecycle_status"] == "EXPIRED" for record in records),
        "recently_updated": recent,
        "average_confidence": sum(assigned) / len(assigned) if assigned else None,
        "assigned_scores": len(assigned),
    }


def _time(value):
    if not value:
        return "Not recorded"
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        return "Timezone not recorded"
    return when.astimezone(IST).strftime("%d %b %Y, %H:%M IST")


def _label(value):
    return value.replace("_", " ").title()


def _dashboard_style(st):
    """Presentation only; data, filters and workflow behavior are unchanged."""
    st.html("""<style>
    .stApp { background: #f6f8fb; color: #172b43; }
    [data-testid="stHeader"] { background: #f6f8fb; }
    [data-testid="stSidebar"] { background: #edf2f7; border-right: 1px solid #dce4ed; }
    [data-testid="stMainBlockContainer"] { padding-top: 2.5rem; padding-bottom: 3rem; max-width: 1600px; }
    h1, h2, h3 { color: #172b43; letter-spacing: -.025em; }
    h1 { font-size: 2.2rem !important; font-weight: 700 !important; }
    h2 { font-size: 1.55rem !important; }
    [data-testid="stMetric"] { background: #fff; border: 1px solid #e0e7ef;
        border-radius: 12px; padding: 18px 16px; box-shadow: 0 2px 8px #172b4305; }
    [data-testid="stMetricLabel"] { color: #52657b; font-size: .85rem; }
    [data-testid="stMetricValue"] { color: #126b72; font-size: 2rem; }
    .stButton button, .stLinkButton a { border-radius: 8px; border: 1px solid #c9d5e2;
        background: #fff; color: #172b43; font-weight: 500; }
    .stButton button:hover, .stLinkButton a:hover { border-color: #126b72; color: #126b72; background: #edf8f7; }
    .stButton button:focus-visible, .stLinkButton a:focus-visible { outline: 2px solid #126b72; outline-offset: 2px; }
    [data-baseweb="select"] > div, [data-testid="stTextInput"] input { background: #fff; border-radius: 8px; }
    [data-baseweb="tab-list"] { gap: 18px; border-bottom: 1px solid #dce4ed; }
    [data-baseweb="tab"] { color: #52657b; }
    [data-baseweb="tab"][aria-selected="true"] { color: #126b72; }
    [data-baseweb="tab-highlight"] { background-color: #126b72; }
    .scholarship-table { overflow: auto; max-height: 500px; background: #fff;
        border: 1px solid #dce4ed; border-radius: 12px; margin: 12px 0 22px; }
    .scholarship-table table { border-collapse: collapse; width: 100%; font-size: .9rem; }
    .scholarship-table th { background: #eaf0f6; color: #243d58; font-weight: 600;
        position: sticky; top: 0; z-index: 1; }
    .scholarship-table td { color: #243d58; }
    .scholarship-table tbody tr:nth-child(even) { background: #f8fafc; }
    .scholarship-table tbody tr:hover { background: #eef7f7; }
    </style>""")


def _table(st, rows):
    """Render escaped cells without importing PyArrow or pandas."""
    if not rows:
        st.caption("No rows to display.")
        return
    columns = list(dict.fromkeys(column for row in rows for column in row))
    style = ('text-align:left;vertical-align:top;padding:10px 12px;'
             'border-bottom:1px solid #e4ebf2;white-space:pre-wrap;'
             'overflow-wrap:break-word;min-width:125px;')
    header = "".join(f'<th scope="col" style="{style}">{escape(str(column))}</th>'
                     for column in columns)
    body = []
    for row in rows:
        cells = []
        for column in columns:
            value = row.get(column, "")
            text = "Unknown" if value is None else str(value)
            cells.append(f'<td style="{style}">{escape(text)}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    st.html('<div class="scholarship-table">'
            '<table style="border-collapse:collapse;width:100%;font-size:.95rem;">'
            '<thead><tr>' + header + '</tr></thead><tbody>'
            + "".join(body) + '</tbody></table></div>')


def _web_url(value):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            parsed.port
            return value
    except ValueError:
        pass
    return None


def read_prepared_source(evidence, review_details, *, project_root=PROJECT_ROOT):
    """Open only the selected stored snapshot and check its source identity."""
    root = Path(project_root).resolve()
    prepared_path = (root / evidence["prepared_content_path"]).resolve()
    raw_path = (root / evidence["raw_file_path"]).resolve()
    if not prepared_path.is_relative_to((root / "data/runtime/snapshots/prepared").resolve()):
        raise ValueError("Prepared snapshot is outside source storage.")
    if not raw_path.is_relative_to((root / "data/runtime/snapshots/raw").resolve()):
        raise ValueError("Raw snapshot is outside source storage.")
    if sha256(raw_path.read_bytes()).hexdigest() != evidence["content_hash"]:
        raise ValueError("Saved raw source failed its integrity check.")
    content = prepared_path.read_bytes()
    from scholarship_intelligence.bundles import load_bundle

    reviews = [review_details] + [entry["previous_review"] for entry in review_details.get("update_history", [])]
    pinned = None
    for review in reviews:
        identity = review.get("storage_identity", {})
        if identity.get("candidate_fingerprint") != evidence["check"].get("candidate_fingerprint"):
            continue
        bundle_id = identity.get("bundle_id")
        if not isinstance(bundle_id, str) or len(bundle_id) != 64 or any(char not in "0123456789abcdef" for char in bundle_id):
            raise ValueError("Stored review has an invalid bundle identity.")
        bundle = load_bundle(root / "data/runtime/bundles" / f"{bundle_id}.json", project_root=root)
        pinned = next((ref for ref in bundle.documents
                       if (root / ref.prepared_file_path).resolve() == prepared_path
                       and ref.source_url == evidence["source_url"]
                       and ref.content_hash == evidence["content_hash"]), None)
        if pinned:
            break
    if pinned is None or sha256(content).hexdigest() != pinned.prepared_content_hash:
        raise ValueError("Prepared source differs from its preserved review bundle.")
    source = json.loads(content)
    expected_id = sha256((evidence["source_url"] + "\0" + evidence["content_hash"]).encode("utf-8")).hexdigest()
    if (source.get("source_url") != evidence["source_url"]
            or source.get("content_hash") != evidence["content_hash"]
            or source.get("snapshot_id") != expected_id):
        raise ValueError("Prepared snapshot identity differs from the stored evidence.")
    if not isinstance(source.get("blocks"), list):
        raise ValueError("Prepared source has no readable blocks.")
    return source, content


def _claim(st, label, claim):
    st.subheader(label)
    value = claim.get("value")
    if value is None:
        st.caption(_label(claim.get("missing_reason") or "not recorded"))
        if claim.get("explanation"):
            st.text(claim["explanation"])
    elif isinstance(value, str):
        st.text(value)
    elif label == "Benefits":
        for benefit in value:
            st.text(benefit["description"])
            if benefit.get("amount") is not None:
                st.caption(f'Amount ({_label(benefit["amount_kind"])}): '
                           f'{benefit["amount"]} {benefit.get("currency") or "currency unspecified"}')
            elif benefit.get("minimum_amount") is not None or benefit.get("maximum_amount") is not None:
                for bound in ("minimum_amount", "maximum_amount"):
                    if benefit.get(bound) is not None:
                        st.caption(f'{_label(bound)}: {benefit[bound]} '
                                   f'{benefit.get("currency") or "currency unspecified"}')
            else:
                st.caption("Monetary amount: " + _label(benefit.get("amount_missing_reason") or "not stated"))
            st.caption("Payment period: " + _label(benefit.get("period", "unspecified")))
            if benefit.get("period_details"):
                st.text(benefit["period_details"])
            for qualifier in benefit.get("qualifiers", []):
                st.text(qualifier)
    elif isinstance(value, dict) and "calendar_date" in value:
        st.text(value["calendar_date"] + " · " + _label(value["stage"]))
        for key in ("route", "time_qualifier"):
            if value.get(key):
                st.text(value[key])
    elif label == "Required documents":
        for document in value:
            st.text(document["name"])
            if document.get("condition"):
                st.caption(document["condition"])
    elif label == "Renewal and continuation":
        st.text(value["summary"])
        for condition in value.get("conditions", []):
            if condition != value["summary"]:
                st.text(condition)
    elif label == "Source status statements":
        for statement in value:
            st.text(statement["statement"])
            st.caption(_label(statement["stage"]))
            if statement.get("route"):
                st.text(statement["route"])
    else:
        st.json(value, expanded=False)


def _facts(st, record):
    claims = record["details"].get("accepted_claims", {})
    # Fact columns remain authoritative for fields stored directly in SQLite.
    columns = {"cycle": "cycle", "benefits": "benefit_json", "eligibility": "eligibility_json",
               "documents": "documents_json", "selection_process": "selection_process",
               "renewal_requirements": "renewal_json"}
    for field, label in (("cycle", "Offering cycle"), ("benefits", "Benefits"),
                         ("eligibility", "Eligibility"), ("opening_date", "Opening date and stage"),
                         ("closing_date", "Closing date and stage"), ("documents", "Required documents"),
                         ("selection_process", "Selection process"),
                         ("renewal_requirements", "Renewal and continuation"),
                         ("status_statements", "Source status statements")):
        claim = dict(claims.get(field, {}))
        if field in columns:
            column = columns[field]
            claim["value"] = _decode(record[column], None) if column.endswith("_json") else record[column]
        if field == "eligibility" and isinstance(claim.get("value"), dict):
            st.subheader(label)
            st.text(claim["value"].get("summary", ""))
            with st.expander("Structured criteria and exceptions"):
                st.json(claim["value"], expanded=True)
        else:
            _claim(st, label, claim)
    application = claims.get("application_url", {})
    st.subheader("Application route")
    url = _web_url(application.get("value"))
    if url:
        st.link_button("Open saved application link", url)
    else:
        st.text(application.get("explanation") or "No accepted application link is recorded.")


def _review(st, record):
    details = record["details"]
    for blocker in details.get("blockers", []):
        st.warning(blocker)
    score = details.get("confidence", {})
    metrics = [{"Metric": _label(metric["group"]), "Weight": f'{metric["weight"]}%',
                "Score": "Unassigned" if metric["score"] is None else f'{metric["score"]:.1f}',
                "Pending checks": metric["pending"]} for metric in score.get("metrics", [])]
    if metrics:
        _table(st, metrics)
    provenance = details.get("extraction_provenance", {})
    if provenance.get("method") == "python_rules":
        st.info("Python rule extraction: " + provenance.get("author", "Unknown rule version") + ". No model API calls.")
    assessment = details.get("assessment", {})
    st.text("Reviewer: " + assessment.get("evaluator", "Not recorded"))
    st.caption("Assessment: " + _time(assessment.get("assessed_at")))
    for check in assessment.get("checks", []):
        with st.expander(f'{_label(check["check_id"])} — {_label(check["status"])}'):
            st.text(check["reason"])
            if check.get("evidence_ids"):
                st.caption("Evidence: " + ", ".join(check["evidence_ids"]))
    drafts = details.get("draft_claims", {})
    if drafts:
        st.subheader("Withheld draft claims")
        for field, claim in drafts.items():
            with st.expander(_label(field)):
                st.json(claim, expanded=False)


def _evidence(st, record):
    include_history = st.checkbox("Include historical evidence", key=f'evidence_history_{record["scholarship_id"]}')
    fingerprint = record["details"].get("storage_identity", {}).get("candidate_fingerprint")
    rows = [row for row in record["evidence"] if include_history or row["is_current"]
            or (row["check"].get("withheld") and row["check"].get("candidate_fingerprint") == fingerprint)]
    fields = sorted({row["field_name"] for row in rows})
    selected = st.selectbox("Evidence field", ["All fields"] + fields,
                            key=f'evidence_field_{record["scholarship_id"]}')
    if selected != "All fields":
        rows = [row for row in rows if row["field_name"] == selected]
    st.caption(f"{len(rows)} field-to-evidence associations. A quote may support several fields.")
    for row in rows:
        if row["check"].get("withheld") and row["check"].get("candidate_fingerprint") == fingerprint:
            status = "Withheld draft"
        else:
            status = "Current" if row["is_current"] else "Historical"
        with st.expander(f'#{row["evidence_id"]} · {row["field_name"]} · {status}'):
            st.text(row["quote"])
            st.caption("Retrieved: " + _time(row["retrieved_at"]))
            st.json(row["location"], expanded=False)
            url = _web_url(row["source_url"])
            if url:
                st.link_button("Open source", url)
    from scholarship_intelligence import preview
    if preview.preparing():
        st.info('Original quoted evidence is shown above. Full source files are being prepared in the background.')
        return
    snapshots = {row["snapshot_id"]: row for row in rows}
    if not snapshots:
        st.info("No evidence matches this selection.")
        return
    snapshot_id = st.selectbox("Saved source to inspect", list(snapshots),
                               format_func=lambda key: snapshots[key]["source_url"],
                               key=f'snapshot_{record["scholarship_id"]}')
    try:
        source, content = read_prepared_source(snapshots[snapshot_id], record["details"])
        blocks = [{"Block": block["block_id"], "Type": block["kind"],
                   "Headings": " > ".join(block.get("heading_path", [])),
                   "Text": block["text"]} for block in source["blocks"]]
        with st.expander("Full saved passages"):
            _table(st, blocks)
        st.download_button("Download prepared source JSON", content,
                           file_name=f'{source["snapshot_id"]}-prepared.json', mime="application/json",
                           key=f'download_{record["scholarship_id"]}_{snapshot_id}')
    except (OSError, ValueError, KeyError, TypeError):
        st.error("The saved source is missing, invalid or inconsistent with its evidence. Inspect the source files locally.")


def load_workflows(database_path=DATABASE_PATH):
    """Latest persisted batch per seed, including drafts awaiting review."""
    from contextlib import closing
    database = Path(database_path).resolve()
    latest = {}
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        for (content,) in connection.execute("SELECT summary_json FROM crawl_runs ORDER BY run_id DESC"):
            data = _decode(content, {})
            if data.get("operation") == "seed_workflow":
                latest.setdefault(data["seed_id"], data)
    return list(latest.values())


def load_workflow_candidate(row, project_root=PROJECT_ROOT):
    root = Path(project_root).resolve()
    path = (root / row["candidate_path"]).resolve()
    if not path.is_relative_to((root / "data/runtime/extractions").resolve()):
        raise ValueError("Draft is outside extraction storage.")
    content = path.read_bytes()
    if sha256(content).hexdigest() != row["candidate_sha256"]:
        raise ValueError("Draft changed since validation; resume the workflow to revalidate it.")
    return json.loads(content)


def scholarship_workflow_rows(report, records):
    """Resolve historical unchanged rows by saved ID, then filter every row."""
    by_id = {record["scholarship_id"]: record for record in records}
    selected = []
    for source in report.get("offerings", []):
        row = dict(source)
        record = by_id.get(row.get("scholarship_id"))
        name = row.get("name") or (record["name"] if record else None)
        claims = ({**record.get("details", {}).get("accepted_claims", {}),
                   **record.get("details", {}).get("draft_claims", {})} if record else {})
        classification = classify_offering(name, claims)
        if classification["main_dashboard_eligible"]:
            row.update(name=name, offering_type="scholarship", offering_classification=classification)
            selected.append(row)
    return selected




def _crawl_controls(st):
    from scholarship_intelligence import workflow_service
    workflow_service.start()
    def controls():
        state = workflow_service.status()
        enabled = st.sidebar.checkbox("Automatic daily crawl", value=state['daily_enabled'], key='daily_crawl_enabled')
        if enabled != state['daily_enabled']:
            state = workflow_service.set_daily(enabled)
        seeds = json.loads((PROJECT_ROOT / 'config/seeds.json').read_bytes())['seeds']
        available = {item['seed_id']: item['label'] for item in seeds if item.get('live_workflow')}
        selected = st.sidebar.selectbox("Crawl source", ['all'] + list(available),
                                        format_func=lambda value: 'All configured sources' if value == 'all' else available[value], key='crawl_source')
        job = state.get('job') or {}
        from scholarship_intelligence import preview
        busy = job.get('status') == 'running' or preview.preparing()
        if st.session_state.get('preview_was_loading') and not preview.preparing():
            st.session_state['preview_was_loading'] = False
            st.rerun()
        if st.sidebar.button("Crawl now", disabled=busy, key='crawl_now'):
            try:
                job = workflow_service.launch(None if selected == 'all' else [selected])
                st.sidebar.success("Crawl started.")
            except (RuntimeError, ValueError, OSError) as error:
                st.sidebar.error(str(error))
        if job:
            progress_path = PROJECT_ROOT / 'data/runtime/live-progress.json'
            if busy and progress_path.exists():
                progress = json.loads(progress_path.read_bytes())
                st.sidebar.caption(str(progress.get('seed_id') or '') + ' · ' + progress.get('phase', '')
                                   + ' · ' + str(progress.get('pages_checked', 0)) + ' pages checked')
            if job['status'] in {'completed', 'completed_with_review'}:
                st.sidebar.success('Crawl finished · ' + _time(job.get('finished_at')))
            if job.get('error'):
                st.sidebar.error(job['error'])
            if job['status'] != 'running' and st.session_state.get('last_completed_crawl') != job['id']:
                st.session_state['last_completed_crawl'] = job['id']
                st.rerun()
    st.fragment(run_every='10s')(controls)()


def main():
    import streamlit as st

    from scholarship_intelligence.storage import initialize_database
    from scholarship_intelligence import preview
    initialize_database()
    preview.start_restore()
    st.set_page_config(page_title="Scholarship Intelligence", page_icon="🎓", layout="wide", menu_items={"Get Help": None, "Report a bug": None, "About": None})
    st.set_option('client.toolbarMode', 'minimal')
    _dashboard_style(st)
    st.title("Scholarship Intelligence")
    origin = PROJECT_ROOT / 'data/runtime/saved-results-origin.json'
    if origin.exists():
        initial = json.loads(origin.read_bytes())
        st.caption('Saved results · original crawl checked ' + _time(initial['source_checked_at']))
    elif preview.preparing():
        manifest = json.loads((PROJECT_ROOT / 'data/samples/saved-results.manifest.json').read_bytes())
        st.caption('Saved results · original crawl checked ' + _time(manifest['source_checked_at']))
    st.sidebar.header("Browse records")
    _crawl_controls(st)
    st.sidebar.button("Refresh saved data")
    try:
        if preview.available():
            all_records = preview.records()
            if preview.preparing():
                st.caption('Preparing saved evidence in the background; the original source dates are preserved.')
                st.session_state['preview_was_loading'] = True
        elif preview.error():
            st.error(preview.error())
            return
        else:
            all_records = load_records()
        records = filter_records(all_records)
    except (sqlite3.Error, OSError, ValueError, KeyError):
        st.error("The existing scholarship database could not be read. Check the local database and stored review data.")
        return
    if not records:
        st.info("No scholarship offerings have been stored yet.")
        return
    search = st.sidebar.text_input("Search name or provider")
    provider = st.sidebar.selectbox("Provider", ["All"] + sorted({record["provider_name"] for record in records}))
    categories = sorted({source["source_type"] for record in records for source in record["sources"]})
    category = st.sidebar.selectbox("Source category", ["All"] + categories, format_func=_label)
    verification = "All"
    lifecycle = st.sidebar.selectbox("Application status", ["All", "ACTIVE", "EXPIRING_SOON", "EXPIRED",
                                                               "REVIEW_REQUIRED", "NO_LONGER_VERIFIABLE"], format_func=_label)
    displayed = filter_records(records, search, provider, category, verification, lifecycle)
    counts = summarize(displayed)
    columns = st.columns(6)
    for column, label, value in zip(columns, ["Total discovered", "Verified", "Review required", "Active", "Expired", "Average confidence"],
                                    [counts["stored"], counts["verified"], counts["review_required"], counts["active"], counts["expired"],
                                     "Unassigned" if counts["average_confidence"] is None else f'{counts["average_confidence"]:.1f}']):
        column.metric(label, value)
    if not displayed:
        st.info("No stored offerings match these filters.")
        return
    _table(st, [{"ID": row["scholarship_id"], "Scholarship": row["name"], "Provider": row["provider_name"],
                   "Offering cycle": row.get("source_candidate", {}).get("cycle", {}).get("value") or row.get("cycle") or "Not established",
                   "Opening date": row["opening_date"] or "Not established", "Closing date": row["closing_date"] or "Not established",
                   "Application mode": row.get("application_mode", "Student application route" if row.get("application_context") else "Not established"),
                   "Availability evidence": "Verified" if row.get("availability_verified") else "Not established",
                   "Official source": "Confirmed" if row.get("source_score", {}).get("source_authenticity_status") == "CONFIRMED" else "Review required",
                   "Source freshness": row.get("source_score", {}).get("source_freshness", "Not assessed"),
                   "Application status": _label(row["lifecycle_status"]),
                   "Factual confidence": "Unassigned" if row["confidence_score"] is None else f'{row["confidence_score"]:.1f}'}
                  for row in displayed])
    lookup = {record["scholarship_id"]: record for record in displayed}
    selected = st.selectbox("Open scholarship", list(lookup),
                            format_func=lambda key: f'{lookup[key]["name"]} — {lookup[key]["provider_name"]} (#{key})')
    record = lookup[selected]
    if record.get("latest_source_check"):
        st.warning(record["latest_source_check"])
    st.header(record["name"])
    st.text(record["provider_name"])
    st.caption(f'Application status: {_label(record["lifecycle_status"])}')
    st.caption("Last verified: " + (_time(record["last_verified_at"]) if record["last_verified_at"] else "Not verified")
               + " · Last meaningful change: " + _time(record["last_changed_at"]))
    if record.get("availability_error"):
        st.error(record["availability_error"])
    if record.get("application_context"):
        context = record["application_context"]
        st.info("Programme-specific student application window: " + context["opening_date"] + " to " + context["closing_date"]
                + (" · Official availability evidence verified." if record["availability_verified"] else " · Re-fetch needed before current availability verification."))
        st.link_button("Official application schedule", context["source_url"])
        for item in context["evidence"]:
            st.text(item["quote"])
            st.caption("Source location: " + item["source_xpath"])
        st.caption(context["scope"])
    if record.get("source_assessment_error"):
        st.error(record["source_assessment_error"])
    facts, latest, review, evidence, sources, history = st.tabs(["Previously accepted facts", "Current source facts", "Historical review", "Historical evidence", "Sources", "History"])
    with latest:
        _latest_source(st, record)
    with facts:
        _facts(st, record)
    with review:
        _review(st, record)
    with evidence:
        _evidence(st, record)
    with sources:
        for source in record["sources"]:
            st.subheader(_label(source["role"]) + " source")
            url = _web_url(source["url"])
            if url:
                st.link_button(source["url"], url)
            st.caption("Category: " + _label(source["source_type"]) + " · Authenticity: "
                       + _label(source["authenticity"].get("status", "unresolved")))
            for basis in source["authenticity"].get("basis", []):
                st.text(basis)
            st.caption("Last successful retrieval: " + _time(source["last_success_at"]))
    with history:
        if not record["changes"]:
            st.info("No accepted changes are recorded. Initial storage is not a change to an existing offering.")
        for change in record["changes"]:
            with st.expander(f'{change["field_name"]} · {_time(change["detected_at"])}'):
                st.text(change["reason"])
                st.write("Previous accepted value")
                st.json(_decode(change["old_value_json"], None))
                st.write("New accepted value")
                st.json(_decode(change["new_value_json"], None))
                st.json({"old_evidence_ids": _decode(change["old_evidence_ids_json"], []),
                         "new_evidence_ids": _decode(change["new_evidence_ids_json"], [])}, expanded=False)



if __name__ == "__main__":
    main()
