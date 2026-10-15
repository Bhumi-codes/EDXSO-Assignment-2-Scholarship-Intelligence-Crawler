"""Deterministic source-section extraction, with no model or API client.

Rules preserve exact source wording. They do not turn eligibility prose into
an automated decision tree or infer an application cycle from recipient rows.
"""

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from scholarship_intelligence.bundles import load_bundle
from scholarship_intelligence.evidence_validation import validate_evidence
from scholarship_intelligence.models import ScholarshipExtraction, classify_offering
from scholarship_intelligence.semantic_assessment import (
    CHECK_DEFINITIONS, CheckResult, SemanticAssessment, candidate_fingerprint,
)
from scholarship_intelligence.settings import PROJECT_ROOT


RULE_VERSION = "python-sections-v15"
SECTION_NAMES = {
    "eligibility": {"eligibility criteria", "eligibility criterion", "eligibiltiy criterion", "eligibility", "academic eligibility"},
    "selection_process": {"selection criteria", "selection criterion", "selection process"},
    "renewal_requirements": {"continuation criteria and duration", "continuation criterion", "continuation criteria", "renewal requirements", "renewal criteria"},
    "benefits": {"scholarship amount and number", "award amount and number", "amount and number", "scholarship amount", "award amount"},
    "documents": {"required documents", "documents required", "list of mandatory documents"},
    "cycle": {"application cycle", "scholarship cycle"},
    "application_instructions": {"how to apply", "application instructions", "application process"},
    "opening_date": {"application opening date", "opening date", "applications open", "application start date", "start date", "scheme open from"},
    "closing_date": {"application closing date", "closing date", "application deadline", "deadline", "last date to apply", "last date of application", "last date", "student application open till", "closure date"},
}
FOOTER_NAMES = {"address", "phone", "email", "useful link", "useful links", "navigation", "contact us", "follow us"}
SECTION_BOUNDARIES = {"donor name", "donor's name", "donor’s name", "scholarship code", "award id", "commencement year"}


def _label(text):
    text = re.sub(r"^\s*\d+[.)]\s*", "", text)
    return re.sub(r"\s+", " ", text.strip().rstrip(":" ).strip()).casefold()


def _source_views(blocks):
    """PDF line views retain their real physical page and parent block ID."""
    views = []
    for block in blocks:
        if block["kind"] != "pdf_page":
            text = block["text"]
            lines = text.splitlines()
            labels = set().union(*SECTION_NAMES.values()) | SECTION_BOUNDARIES
            first, separator, remainder = text.partition(":")
            if block["kind"] in {"paragraph", "text"} and separator and _label(first) in labels:
                pieces = [first + ":", remainder.strip()]
            elif block["kind"] in {"paragraph", "text"} and len(lines) > 1 and any(_label(line) in labels for line in lines):
                pieces = [line.strip() for line in lines]
            else:
                pieces = None
            if pieces:
                for piece in pieces:
                    if piece:
                        views.append({**block, "text": piece,
                            "links": [link for link in block.get("links", []) if link.get("text") and link["text"] in piece]})
            else:
                views.append(block)
            continue
        pending = []
        def flush():
            if pending:
                views.append({**block, "text": "\n".join(pending).strip(), "kind": "paragraph"})
                pending.clear()
        for line in block["text"].splitlines():
            text = line.strip()
            if not text:
                flush()
                continue
            if re.fullmatch(r"Page\s+\d+\s+of\s+\d+", text, re.I):
                flush()
                continue
            label = _label(text)
            from scholarship_intelligence.source_profiles import section_role
            before, separator, after = line.partition(":")
            if separator and section_role(before):
                flush()
                views.append({**block, "text": before.strip() + ":", "kind": "heading"})
                if after.strip():
                    pending.append(after)
                continue
            is_label = any(label in labels for labels in SECTION_NAMES.values()) or section_role(text) is not None
            is_title = len(text) <= 200 and text.isupper() and bool(re.search(r"\b(SCHOLARSHIPS?|AWARDS?|FELLOWSHIPS?)\b", text))
            is_boundary = len(text) <= 160 and bool(re.fullmatch(r"(?:\d+(?:\.\d+)*[.)]?|\([ivx]+\))\s+[A-Z][A-Z /(),:&-]+[:.]?", text))
            is_question = bool(re.match(r"^(?:Q\.?\s*\d+\s*[:.)]?\s+|\d+[.)]\s+(?=what|who|which|how|if|is|are|can|do|does))", text, re.I))
            if is_label or is_title or is_boundary or is_question:
                flush()
                views.append({**block, "text": text, "kind": "heading"})
            else:
                if re.match(r"^(?:\d+[.)]|[a-z][.)]|[\u2022\u25aa])\s+", text):
                    flush()
                pending.append(line)
        flush()
    return views


def _section_blocks(blocks):
    """Labels can be paragraphs on IIT pages, not just HTML headings."""
    sections = {field: [] for field in SECTION_NAMES}
    active = None
    for block in blocks:
        label = _label(block["text"])
        field = next((name for name, labels in SECTION_NAMES.items() if label in labels), None)
        if field:
            active = field
        elif label in FOOTER_NAMES | SECTION_BOUNDARIES or block["kind"] == "table" or (block["kind"] == "heading" and active):
            active = None
        elif active and block["kind"] != "table" and block["text"].strip() and label not in {"icon", "top"}:
            sections[active].append(block)
    return sections


def _benefit(text):
    # Only an unqualified single rupee amount in a labelled benefit section
    # becomes a fixed amount. Everything else retains its literal wording.
    amount = None
    period = "unspecified"
    fixed = re.fullmatch(r"\s*(?:Rs\.?|INR|₹)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(lakh(?:s)?|lac(?:s)?|crore(?:s)?|thousand)?\s*(?:/-)?\s*(pa|p\.?a\.?|per annum|annually|per year|annual|pm|p\.?m\.?|per\s+month|monthly|one time|one-time)?\s*", text, re.I)
    if fixed:
        digits = fixed[1]
        if "," not in digits or re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d+)?", digits):
            multiplier = 100000 if fixed[2] and fixed[2].lower().startswith(("lakh", "lac")) else 10000000 if fixed[2] and fixed[2].lower().startswith("crore") else 1000 if fixed[2] else 1
            amount = str(Decimal(digits.replace(",", "")) * multiplier)
            suffix = re.sub(r"\s+", " ", (fixed[3] or "").lower().replace(".", ""))
            period = "monthly" if suffix in {"pm", "per month", "monthly"} else "one_time" if suffix in {"one time", "one-time"} else "annual" if suffix else "unspecified"
    kind = "fixed" if amount is not None else "unspecified"
    # A single explicitly stated amount with prose qualifiers remains bound
    # to the complete quoted description; no amount is split among recipients.
    money = list(re.finditer(r"(?:Rs\.?|INR|₹)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(lakh(?:s)?|lac(?:s)?|crore(?:s)?|thousand)?", text, re.I))
    if amount is None and len(money) == 1 and not re.search(r"\d\s*[-–]\s*\d|depending on|between", text, re.I):
        digits, scale = money[0].groups()
        multiplier = 100000 if scale and scale.lower().startswith(("lakh", "lac")) else 10000000 if scale and scale.lower().startswith("crore") else 1000 if scale else 1
        amount = str(Decimal(digits.replace(",", "")) * multiplier)
        prefix = text[:money[0].start()][-50:]
        kind = "maximum" if re.search(r"up to|upto|maximum", prefix, re.I) else "minimum" if re.search(r"at least|minimum", prefix, re.I) else "fixed"
        period = "monthly" if re.search(r"\b(?:per\s+month|monthly|p\.?m\.?)\b", text, re.I) else "annual" if re.search(r"\b(?:per\s+annum|per\s+year|annual|annually|p\.?a\.?)\b", text, re.I) else "whole_course" if re.search(r"duration of (?:the |their )?(?:course|programme)|whole course", text, re.I) else "unspecified"
    elif len(money) > 1:
        kind = "variable"
    if period == "unspecified":
        period = "monthly" if re.search(r"\b(?:monthly|per\s+month|p\.?m\.?)\b", text, re.I) else "annual" if re.search(r"\b(?:annually|annual|per\s+annum|per\s+year|p\.?a\.?)\b", text, re.I) else "one_time" if re.search(r"\bone[- ]time\b", text, re.I) else "whole_course" if re.search(r"duration of (?:the |their )?(?:course|programme)|whole course", text, re.I) else "unspecified"
    return {
        "description": text, "amount_kind": kind,
        "amount": amount, "minimum_amount": None, "maximum_amount": None,
        "amount_missing_reason": None if amount is not None else "ambiguous_or_conflicting" if money else "not_specified_in_inspected_material",
        "currency": "INR" if amount is not None else None, "period": period,
        "period_details": None, "qualifiers": [],
    }


MONTHS = {name: number for number, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1)}
MONTH_NAMES = "|".join(MONTHS) + "|" + "|".join(name[:3] for name in MONTHS) + "|sept"
DATE_PATTERN = re.compile(
    rf"(?<![\d/.-])(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{4}}|"
    rf"\d{{1,2}}(?:st|nd|rd|th)?[\s-]+(?:{MONTH_NAMES})\.?[\s,-]+\d{{4}}|"
    rf"(?:{MONTH_NAMES})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?[\s,]+\d{{4}})(?!\d)", re.I)
OPEN_CUE = re.compile(r"\b(?:scheme\s+open\s+from|opening date|start date|applications?\s+(?:(?:(?:will|are|is)\s+)?(?:open|start|begin)(?:s|ed)?|opening)|application\s+(?:process|period|window|submission)\s+(?:opens?|starts?|begins?|began)|accepting applications\s+(?:from|on)|application season\s+(?:will\s+)?begin|application season\s+(?:opens?|starts?|begins?))\b", re.I)
CLOSE_CUE = re.compile(r"\b(?:student\s+application\s+open\s+(?:till|until)|applications?\s+(?:(?:are|is)\s+)?open\s+(?:till|until)|closure date|last date of application|last date(?=\s*:)|closing date|close date|application deadline|last date\s+(?:to apply|for\s+(?:applications?|submission))|deadline\s+(?:for|to)\s+(?:applications?|applying|apply|submission)|applications?\s+(?:close[sd]?|end[sd]?)|apply\s+(?:by|before)|application\s+(?:window|submission|period)\s+(?:closes?|ends?)|extended\s+(?:until|to|till))\b", re.I)
OPEN_CUE = re.compile(OPEN_CUE.pattern + r"(?!\s+(?:till|until))", re.I)
WINDOW_CUE = re.compile(r"\bapplication\s+(?:period|window|dates|schedule)\b", re.I)
NON_APPLICATION = re.compile(r"\b(?:date of birth|born|copyright|viva|publication date|published on|issued on|funding starts?|study starts?|course starts?|payment|disbursement|academic year|recipient|awardee)\b", re.I)


def _date(text, *, date_order=None):
    from datetime import date
    raw = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", text.strip(), flags=re.I)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            value = date.fromisoformat(raw)
        elif (match := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", raw)):
            first, second, year = map(int, match.groups())
            if first == second or first > 12 or date_order == "DMY":
                day, month = first, second
            elif second > 12 or date_order == "MDY":
                month, day = first, second
            else:
                return None
            value = date(year, month, day)
        else:
            match = re.fullmatch(rf"(\d{{1,2}})[\s-]+({MONTH_NAMES})\.?[\s,-]+(\d{{4}})", raw, re.I)
            if match:
                day, name, year = match.groups()
            else:
                match = re.fullmatch(rf"({MONTH_NAMES})\.?\s+(\d{{1,2}})[\s,]+(\d{{4}})", raw, re.I)
                if not match:
                    return None
                name, day, year = match.groups()
            month = next(number for full, number in MONTHS.items() if full.startswith(name.lower().rstrip('.')))
            value = date(int(year), month, int(day))
    except (ValueError, StopIteration):
        return None
    return {"calendar_date": value.isoformat(), "stage": "student_application", "route": None, "time_qualifier": None}


def _date_segments(block):
    if block.get("kind") == "table" and block.get("table_rows"):
        for row in block["table_rows"]:
            yield "\n".join(cell["text"] for cell in row)
    elif block.get("kind") == "pdf_page":
        yield from re.split(r"\n\s*\n", block["text"])
    else:
        # Keep one sentence/line per context. A nearby publication or renewal
        # date is not silently associated with another application deadline.
        actor_labels = r"(?=(?:Scheme\s+Open\s+from|Student\s+Application\s+Open\s+till|Defective\s+Application\s+Verification|Institute\s+Verification|DNO/SNO/MNO\s+Verification))"
        for piece in re.split(actor_labels, block["text"], flags=re.I):
            yield from re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z])", piece)


def _application_dates(documents, refs, *, date_order=None):
    found = {"opening_date": [], "closing_date": []}
    diagnostics = []
    for url, document in documents.items():
        previous_label = ""
        for block in document["blocks"]:
            source = {**block, "_snapshot_id": refs[url].snapshot_id}
            if _label(block["text"]) in FOOTER_NAMES:
                previous_label = ""
                continue
            for segment in _date_segments(block):
                own_cue = OPEN_CUE.search(segment) or CLOSE_CUE.search(segment)
                context = segment if own_cue else previous_label + " " + segment
                matches = list(DATE_PATTERN.finditer(segment))
                closing_cue = re.compile(CLOSE_CUE.pattern + r"|\band\s+closes?\b", re.I) if OPEN_CUE.search(segment) else CLOSE_CUE
                opening, closing = OPEN_CUE.search(context), closing_cue.search(context)
                if previous_label and _label(previous_label) == "deadline":
                    closing = re.search(r"deadline", context, re.I)
                window = WINDOW_CUE.search(context)
                if (opening or closing or window) and not matches:
                    diagnostics.append({"source_url": url, "snapshot_id": source["_snapshot_id"], "block_id": block["block_id"], "page_number": block.get("page_number"), "context": context, "stage": "unspecified", "date_text": None, "reason": "Application event found, but no complete supported date with an explicit year was found."})
                if matches and (opening or closing or window):
                    stage = ("institution_verification" if re.search(r"\b(?:institute|institution|school|district|nodal)\b.*\bverif", context, re.I)
                             else "renewal_application" if re.search(r"\brenewal\b", context, re.I)
                             else "nomination" if re.search(r"\bnomination\b", context, re.I)
                             else "student_application")
                    excluded = stage != "student_application" or NON_APPLICATION.search(context)
                    for match in matches:
                        parsed = _date(match[0], date_order=date_order)
                        # Match each date to its nearest preceding cue. This
                        # supports "opens on ... and closes on ..." in prose.
                        before = segment[:match.start()]
                        opens = list(OPEN_CUE.finditer(before))
                        closes = list(closing_cue.finditer(before))
                        kind = ("opening_date" if opens and (not closes or opens[-1].start() > closes[-1].start())
                                else "closing_date" if closes else "opening_date" if opening and not closing
                                else "closing_date" if closing and not opening else None)
                        if kind is None and window and len(matches) == 2 and re.search(r"\b(?:to|until|through|till)\b", segment[matches[0].end():matches[1].start()], re.I):
                            kind = "opening_date" if match is matches[0] else "closing_date"
                        if excluded or parsed is None or kind is None:
                            diagnostics.append({"source_url": url, "snapshot_id": source["_snapshot_id"],
                                "block_id": block["block_id"], "page_number": block.get("page_number"),
                                "date_text": match[0], "context": context, "stage": stage,
                                "reason": "Non-student or non-application date" if excluded else "Ambiguous date format or date-to-event association"})
                        else:
                            # A stated time/route may change the cutoff meaning;
                            # keep the literal context as a qualifier for review.
                            next_cue = min([m.start() for cue in (OPEN_CUE, closing_cue) for m in cue.finditer(segment) if m.start() > match.end()] or [len(segment)])
                            time_context = segment[match.end():next_cue]
                            if re.search(r"\b(?:\d{1,2}:\d{2}|AM|PM|IST|UTC|GMT)\b", time_context, re.I):
                                parsed["time_qualifier"] = time_context.strip()
                            found[kind].append((parsed, source))
                label = _label(segment)
                if label in SECTION_NAMES["opening_date"] | SECTION_NAMES["closing_date"]:
                    previous_label = segment
                elif segment.strip():
                    previous_label = ""
    return found, diagnostics


def extract_bundle(bundle_file, seed, primary_url, *, project_root=PROJECT_ROOT):
    root = Path(project_root).resolve()
    bundle = load_bundle(bundle_file, project_root=root)
    refs = {ref.source_url: ref for ref in bundle.documents}
    if primary_url not in refs or urlsplit(primary_url).hostname not in seed["allowed_hosts"]:
        raise ValueError("Primary source is not in this bundle's configured seed scope.")
    documents = {url: json.loads((root / ref.prepared_file_path).read_bytes()) for url, ref in refs.items()}
    main, ref = documents[primary_url], refs[primary_url]
    from scholarship_intelligence.source_profiles import scoped_blocks, source_sections
    scope = seed.get("source_scope")
    main_blocks = scoped_blocks(main["blocks"], scope)
    blocks = _source_views(main_blocks)
    ids = [document.snapshot_id for document in bundle.documents]
    payload = {"schema_version": "1.0", "bundle_id": bundle.bundle_id, "snapshot_ids": ids, "evidence": []}
    for field in ScholarshipExtraction.model_fields:
        if field not in payload:
            payload[field] = {"value": None, "missing_reason": "extraction_failed",
                              "explanation": "The current Python rules could not establish this field; inspect the saved source.",
                              "evidence_ids": [], "inspected_snapshot_ids": ids}

    def claim(field, value, cited):
        previous = set(payload[field]["evidence_ids"])
        payload["evidence"] = [entry for entry in payload["evidence"] if entry["evidence_id"] not in previous]
        evidence_ids = []
        for number, block in enumerate(cited, 1):
            identifier = "py_" + field + "_" + block.get("_snapshot_id", ref.snapshot_id)[:8] + "_" + block["block_id"] + "_" + str(number)
            evidence_ids.append(identifier)
            payload["evidence"].append({"evidence_id": identifier, "snapshot_id": block.get("_snapshot_id", ref.snapshot_id),
                "quote": block["text"], "location": {"block_ids": [block["block_id"]],
                "page_number": block.get("page_number"), "section": None}, "field_paths": [field]})
        payload[field] = {"value": value, "missing_reason": None, "explanation": None,
                          "evidence_ids": evidence_ids, "inspected_snapshot_ids": list(dict.fromkeys(block.get("_snapshot_id", ref.snapshot_id) for block in cited))}

    title_views = []
    if scope:
        for block in blocks:
            match = re.match(r"\s*" + r"\s+".join(re.escape(word) for word in scope["name"].split()), block["text"])
            if match and (scope["title_xpath"] == block.get("source_xpath") or scope["title_xpath"].startswith(block.get("source_xpath", "!") + "/")):
                title_views.append({**block, "text": match[0].strip()})
    titles = (title_views if scope else
              [block for block in blocks if block["kind"] == "heading" and re.search(r"\b(scholarships?|awards?|medals?|prizes?|fellowships?)\b", block["text"], re.I)])
    if not scope and seed.get("extraction_profile") != "iit_alumni_v1":
        primary_titles = [block for block in blocks if block["kind"] == "heading" and re.search(r"/h1(?:\[\d+\])?$", block.get("source_xpath", ""))]
        if primary_titles:
            titles = primary_titles
    if not titles and seed.get("extraction_profile") == "iit_alumni_v1":
        section_labels = set().union(*SECTION_NAMES.values()) | FOOTER_NAMES | SECTION_BOUNDARIES
        titles = [block for block in blocks if block["kind"] == "heading" and _label(block["text"]) not in section_labels]
    if titles and len(titles[0]["text"]) <= 500:
        designations = [block for block in blocks if re.match(r"^(?:Scholarship Code|Award ID)\s*:", block["text"], re.I)]
        claim("scholarship_name", titles[0]["text"], [titles[0]] + designations)
        if designations:
            markers = list(dict.fromkeys(re.match(r"^(Scholarship Code|Award ID)", block["text"], re.I)[1].lower() for block in designations))
            payload["scholarship_name"]["explanation"] = "Source programme designation: " + ", ".join(markers) + "."
        claim("official_source", primary_url, [titles[0]])
    provider = scope.get("provider") if scope else seed.get("provider_name")
    provider_blocks = [block for block in main["blocks"] if provider and provider.casefold() in block["text"].casefold()
                       and (not scope or not scope.get("provider_xpath") or block.get("source_xpath") == scope["provider_xpath"]
                            or scope["provider_xpath"].startswith(block.get("source_xpath", "!") + "/"))]
    if provider_blocks:
        claim("provider", provider, [provider_blocks[-1]])

    sections = _section_blocks(blocks) if seed.get("extraction_profile") == "iit_alumni_v1" else source_sections(blocks)
    if seed.get("extraction_profile") == "iit_alumni_v1":
        from scholarship_intelligence.source_profiles import inline_role
        for block in blocks:
            if inline_role(block["text"]) == "documents" and block not in sections.get("documents", []):
                sections.setdefault("documents", []).append(block)
    if seed.get("extraction_profile") != "iit_alumni_v1":
        for url, doc in documents.items():
            if url == primary_url:
                continue
            views = [{**block, "_snapshot_id": refs[url].snapshot_id} for block in _source_views(doc["blocks"])]
            for field, cited in source_sections(views).items():
                sections.setdefault(field, []).extend(cited)
    for field, cited in sections.items():
        if not cited:
            continue
        text = "\n".join(block["text"] for block in cited)
        if field == "benefits":
            # One description per source block avoids merging different awards.
            claim(field, [_benefit(part.strip()) for block in cited for part in block["text"].split("|") if part.strip()], cited)
        elif field == "eligibility":
            claim(field, {"summary": text, "conditions": {"kind": "condition", "category": "other",
                "statement": text, "numeric": None, "qualifiers": []}, "exceptions": []}, cited)
        elif field == "selection_process":
            claim(field, text, cited)
        elif field == "renewal_requirements":
            claim(field, {"summary": text, "conditions": [block["text"] for block in cited]}, cited)
        elif field == "documents":
            claim(field, [{"name": block["text"], "condition": "Institution verification" if re.search(r"institution(?:s)?.{0,100}(?:upload|verify)", re.sub(r"\s+", " ", block["text"]), re.I) else None} for block in cited], cited)
        elif field == "application_instructions":
            no_application = [block for block in cited if _label(block["text"]) in {"no application needed", "no application required"}]
            if no_application:
                claim("status_statements", [{"statement": block["text"], "stage": "student_application"} for block in no_application], no_application)
                claim("application_url", None, no_application)
                payload["application_url"].update(missing_reason="not_applicable",
                    explanation="The inspected page explicitly states that no application is needed. This is source guidance, not verification of a current offering or application window.")
        elif field == "cycle" and len(cited) == 1 and re.fullmatch(r"20\d{2}(?:\s*[-/]\s*(?:20)?\d{2})?", text.strip()):
            claim(field, text.strip(), cited)
    date_documents = {**documents, primary_url: {**main, "blocks": main_blocks}}
    dates, date_diagnostics = _application_dates(date_documents, refs, date_order=seed.get("date_order"))
    for field, proposals in dates.items():
        values = {json.dumps(value, sort_keys=True) for value, _ in proposals}
        if len(values) == 1:
            value = proposals[0][0]
            cited = list({(block.get("_snapshot_id"), block["block_id"]): block for _, block in proposals}.values())
            claim(field, value, cited)
        elif len(values) > 1:
            payload[field].update(missing_reason="ambiguous_or_conflicting", explanation="The inspected sources contain multiple application dates or qualifiers; establish the applicable notice, route and cycle before accepting one.")
        else:
            payload[field].update(explanation="No supported application date was identified in the inspected bundle. A parser miss or a separate official notice remains possible; this is not proof that the provider has no date.")
    opening, closing = payload["opening_date"]["value"], payload["closing_date"]["value"]
    if opening and closing and closing["calendar_date"] < opening["calendar_date"]:
        for field in ("opening_date", "closing_date"):
            payload[field].update(value=None, missing_reason="ambiguous_or_conflicting", explanation="The extracted application closing date precedes the opening date; check the source's cycle and route.")
    # Application links must appear in the primary source, not be invented from
    # a login, footer link, third-party domain or a query-string keyword.
    routes = []
    route_blocks = main["blocks"] if seed.get("extraction_profile") == "nsp_cards_v1" else blocks
    for block in route_blocks:
        for link in block.get("links", []):
            if _label(link.get("text", "")) in {"apply now", "apply online", "application form", "apply", "apply for scholarship"}:
                url = link.get("url", "")
                parsed = urlsplit(url)
                if parsed.scheme == "https" and parsed.hostname in seed["allowed_hosts"] + seed.get("application_hosts", []):
                    routes.append((url, block))
    if len({url for url, _ in routes}) == 1:
        if payload["application_url"]["missing_reason"] == "not_applicable":
            payload["application_url"].update(missing_reason="ambiguous_or_conflicting",
                explanation="The page states no application is needed but also provides an application link; establish the applicable route before accepting one.")
        else:
            claim("application_url", routes[0][0], [routes[0][1]])

    if seed.get("extraction_profile") != "iit_alumni_v1":
        if payload["scholarship_name"]["value"] and not re.search(r"scholarship|award|fellowship", payload["scholarship_name"]["value"], re.I):
            designations = [block for block in main["blocks"] if re.search(r"scholarships? (?:initiative|programme|program)", block["text"], re.I)]
            if designations:
                name = payload["scholarship_name"]["value"]
                claim("scholarship_name", name, [titles[0], designations[0]])
                payload["scholarship_name"]["explanation"] = "Source programme designation: scholarship initiative."
        # A cycle must be explicitly described on this programme's own page.
        # A filename, recipient year or calendar date cannot supply it.
        cycles = []
        for block in blocks:
            if re.search(r"academic year|programme|program|scholarships?|cycle", block["text"], re.I):
                values = re.findall(r"\b20\d{2}\s*[-–/]\s*(?:20)?\d{2}\b", block["text"])
                cycles.extend((value, block) for value in values)
        if len({value for value, _ in cycles}) == 1:
            claim("cycle", cycles[0][0], [cycles[0][1]])
        statements = [block for block in blocks if re.fullmatch(r"Applications? (?:are |now |currently )?(?:open|closed)[.!]?", block["text"], re.I)]
        if statements:
            claim("status_statements", [{"statement": block["text"], "stage": "student_application"} for block in statements], statements)
        for field in ("documents", "selection_process", "renewal_requirements"):
            if payload[field]["value"] is None and not sections.get(field):
                payload[field].update(missing_reason="not_specified_in_inspected_material", explanation="No explicit source section for this field was identified; other provider material may still contain it.")

    candidate = ScholarshipExtraction.model_validate(payload)
    evidence = validate_evidence(candidate, root / bundle.bundle_file_path, project_root=root)
    if not evidence.evidence_checks_passed:
        raise ValueError("Python extraction failed deterministic evidence validation.")
    details = {
        "rule_version": RULE_VERSION, "profile": seed.get("extraction_profile", "labelled_sections"),
        "application_date_candidates": {field: [value for value, _ in entries] for field, entries in dates.items()},
        "date_review_items": date_diagnostics,
        "sections_found": {field: len(items) for field, items in sections.items() if items},
        "documents_inspected": len(documents), "blocks_inspected": sum(len(d["blocks"]) for d in documents.values()),
        "eligibility_representation": "literal_source_text_not_an_automated_decision_tree",
        "unparsed_fields": [field for field, value in payload.items() if isinstance(value, dict) and value.get("missing_reason")],
        "api_calls_made": 0,
        "source_scope": scope,
    }
    title = candidate.scholarship_name.value or ""
    details["offering_classification"] = classify_offering(title, candidate.model_dump(mode="json"))
    identity_reasons = []
    if re.search(r"\blecture award\b", title, re.I):
        identity_reasons.append("A lecture award may target faculty or researchers rather than student scholarship applicants; confirm beneficiary relevance.")
    if re.search(r"\baward\b", title, re.I) and re.search(r"\bfellowship\b", title, re.I):
        identity_reasons.append("The page title contains both an award and a fellowship; separate their programme identities before importing.")
    if identity_reasons:
        details["programme_identity_review_reasons"] = identity_reasons
    return candidate, details


def assess_candidate(candidate, extraction_details, evidence_report, seed, *, assessed_at=None):
    """Assess supported machine checks and identify concrete review gaps."""
    if (not evidence_report.evidence_checks_passed
            or evidence_report.bundle_id != candidate.bundle_id):
        raise ValueError("Assessment requires a passing evidence report for this bundle.")
    checks = []
    rule_checks = {
        "source_integrity": ("pass", "Raw and prepared source hashes passed bundle integrity checks."),
        "evidence_integrity": ("pass", "Exact quote, block, page, field and URL checks passed."),
    }
    refs = {}
    fields = {name: getattr(candidate, name) for name in type(candidate).model_fields
              if hasattr(getattr(candidate, name), "missing_reason")}
    missing = [name for name, claim in fields.items() if claim.value is None]
    conflicts = [name for name, claim in fields.items()
                 if claim.missing_reason == "ambiguous_or_conflicting"]
    identity = extraction_details.get("programme_identity_review_reasons", [])
    unmatched = extraction_details.get("supporting_source_review", [])
    rule_checks["completeness"] = ("pending", "Fields unresolved by the parser: "
        + (", ".join(missing) or "none")
        + ". Extracted sections do not establish that every requirement was captured."
        + (" Supporting links still need retrieval or offering association." if unmatched else ""))
    rule_checks["critical_claim_support"] = ("pending",
        "Eligibility is preserved as quoted prose; conditions, exceptions and beneficiary scope have not been evaluated."
        if candidate.eligibility.value is not None else
        "Eligibility was not established. Review the relevant source section or supporting notice.")
    rule_checks["conflict_resolution"] = (
        "fail" if conflicts or identity else "pending",
        "Unresolved source fields: " + ", ".join(conflicts) + ". " + " ".join(identity)
        if conflicts or identity else
        "No parser-detected conflict; source wording, overlapping conditions and any additional notices still require contextual comparison.")
    route = candidate.application_url
    rule_checks["application_route_support"] = ("pending",
        "No unique supported application link was extracted; inspect the provider's application notice. A missing link is not evidence that applications are closed.")
    if route.value is None and route.missing_reason == "not_applicable" and route.evidence_ids:
        statements = candidate.status_statements.value or []
        if any(_label(item.statement) in {"no application needed", "no application required"} for item in statements):
            rule_checks["application_route_support"] = ("not_applicable",
                "The source explicitly states no application is needed. This does not establish a current cycle or active availability.")
            refs["application_route_support"] = tuple(route.evidence_ids)
    if route.value is not None and route.evidence_ids:
        parsed = urlsplit(str(route.value))
        if parsed.scheme == "https" and parsed.hostname in seed["allowed_hosts"]:
            rule_checks["application_route_support"] = ("pass",
                "A uniquely labelled HTTPS application link on the configured official host passed saved-source URL validation. This does not establish that the form is currently open.")
            refs["application_route_support"] = tuple(route.evidence_ids)
    dates = [candidate.opening_date, candidate.closing_date]
    rule_checks["offering_cycle_context"] = ("pending",
        "An explicit application cycle was not established; recipient years and publication years cannot supply it."
        if candidate.cycle.value is None else
        "An application cycle was extracted, but its association with the application notice and student route still needs review.")
    rule_checks["availability_support"] = ("pending",
        "No supported student application window was established; active or expired cannot be inferred."
        if not all(claim.value is not None for claim in dates) else
        "Application dates were extracted. Confirm their cycle, route and applicability before assigning active or expired.")
    numeric_gaps = []
    if candidate.eligibility.value is not None:
        numeric_gaps.append("eligibility numbers and units remain literal prose")
    for item in candidate.benefits.value or []:
        expected = _benefit(item.description)
        if (item.amount is None or str(item.amount) != expected["amount"]
                or item.currency != expected["currency"] or item.amount_kind != "fixed"):
            numeric_gaps.append("a benefit has no unambiguous source-matched fixed INR amount")
    if extraction_details.get("date_review_items") or any(name in conflicts for name in ("opening_date", "closing_date")):
        numeric_gaps.append("application dates have unresolved context or competing values")
    if numeric_gaps:
        rule_checks["numbers_dates_units"] = ("pending", "; ".join(dict.fromkeys(numeric_gaps)) + ".")
    else:
        numeric_claims = [name for name in ("benefits", "opening_date", "closing_date") if fields[name].value is not None]
        if numeric_claims:
            rule_checks["numbers_dates_units"] = ("pass",
                "Asserted amounts use the strict fixed-INR parser; asserted dates passed calendar, ambiguity and ordering checks. Unstated periods and dates remain unknown.")
            refs["numbers_dates_units"] = tuple(dict.fromkeys(eid for name in numeric_claims for eid in fields[name].evidence_ids))
        else:
            rule_checks["numbers_dates_units"] = ("pending", "No normalized amounts or application dates were established; inspect source numbers before assessing units.")
    for check_id in CHECK_DEFINITIONS:
        status, reason = rule_checks.get(check_id, ("pending", "Configured provider ownership has not been established by a supported source profile."))
        if check_id == "primary_source_ownership" and seed.get("extraction_profile") == "iit_alumni_v1" and candidate.provider.value == "IIT Delhi" and urlsplit(str(candidate.official_source.value)).hostname == "alumni.iitd.ac.in":
            status, reason = "pass", "Configured IIT Delhi alumni scope and the source's IIT Delhi provider text agree."
            refs[check_id] = tuple(candidate.provider.evidence_ids)
        checks.append(CheckResult(check_id=check_id, status=status, reason=reason, evidence_ids=refs.get(check_id, ())))
    withheld = tuple(field for field in ("eligibility",) if getattr(candidate, field).value is not None)
    return SemanticAssessment(
        bundle_id=candidate.bundle_id, snapshot_ids=tuple(candidate.snapshot_ids),
        candidate_fingerprint=candidate_fingerprint(candidate),
        assessed_at=assessed_at or datetime.now(timezone.utc), evaluator=RULE_VERSION,
        checks=tuple(checks), withheld_fields=withheld,
    )


def save_draft(candidate, extraction_details, *, project_root=PROJECT_ROOT):
    """Idempotent extraction artifact with explicitly software provenance."""
    root = Path(project_root).resolve()
    rule_hash = sha256((RULE_VERSION + json.dumps(extraction_details, sort_keys=True)).encode()).hexdigest()
    directory = root / "data/runtime/extractions" / ("python-" + candidate.bundle_id + "-" + rule_hash[:12])
    content = (json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n").encode()
    existing = directory / "candidate.json"
    if existing.exists():
        if existing.read_bytes() != content:
            raise ValueError("An existing Python draft changed; inspect it before continuing.")
        return directory
    directory.mkdir(parents=True, exist_ok=False)
    provenance = {"version": "python-extraction-v1", "method": "python_rules", "author": RULE_VERSION,
        "author_kind": "software", "created_at": datetime.now(timezone.utc).isoformat(),
        "bundle_id": candidate.bundle_id, "snapshot_ids": candidate.snapshot_ids,
        "candidate_sha256": sha256(content).hexdigest(), "candidate_fingerprint": candidate_fingerprint(candidate), "api_calls_made": 0}
    try:
        existing.write_bytes(content)
        (directory / "python-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
        (directory / "extraction-details.json").write_text(json.dumps(extraction_details, indent=2) + "\n", encoding="utf-8")
    except BaseException:
        for name in ("candidate.json", "python-provenance.json", "extraction-details.json"):
            (directory / name).unlink(missing_ok=True)
        directory.rmdir()
        raise
    return directory
