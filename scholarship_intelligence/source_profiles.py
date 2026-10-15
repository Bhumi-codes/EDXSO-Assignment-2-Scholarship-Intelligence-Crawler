"""Structural scope and section rules for official multi-programme websites.

No scholarship facts live here. Titles, agencies, dates and amounts come from
the fetched response. Shared pages require explicit programme association.
"""
from hashlib import sha256
import re
from urllib.parse import urljoin


def programme_key(name):
    name = re.sub(r"\((?:Merit|Welfare) Based Scheme\)", "", name or "", flags=re.I)
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def discover_scopes(content, seed):
    from lxml import html
    tree = html.fromstring(content)
    profile = seed.get("extraction_profile")
    output = []
    if profile == "nsp_cards_v1":
        for heading in tree.xpath("//h6"):
            card = heading.getparent()
            title = " ".join(heading.text_content().split())
            if not card.xpath(".//span[contains(.,'Student Application')]"):
                continue
            agencies = heading.xpath("ancestor::*[contains(concat(' ',normalize-space(@class),' '),' accordion-item ')][1]//*[contains(concat(' ',normalize-space(@class),' '),' accordion-header ')]/button")
            if not agencies:
                continue
            agency = agencies[0]
            import requests
            links = [requests.Request("GET", urljoin(seed["start_url"], node.get("href", ""))).prepare().url for node in card.xpath(".//a[@href]")
                     if node.get("href") and node.get("href") != "null" and "not_available.pdf" not in node.get("href", "")]
            output.append({"name": title, "provider": " ".join(agency.text_content().split()),
                "card_text_sha256": sha256(' '.join(card.text_content().split()).encode()).hexdigest(),
                "card_xpath": tree.getroottree().getpath(card), "title_xpath": tree.getroottree().getpath(heading),
                "provider_xpath": tree.getroottree().getpath(agency), "support_urls": list(dict.fromkeys(links))})
    elif profile == "hdfc_cards_v1":
        for card in tree.xpath("//*[contains(concat(' ',normalize-space(@class),' '),' accordion-item ')]"):
            if not card.xpath(".//h3[normalize-space(.)='Eligibility']"):
                continue
            heading = card.xpath(".//button[1]")[0]
            title = " ".join(heading.text_content().split())
            output.append({"name": title, "provider": seed["provider_name"],
                "card_xpath": tree.getroottree().getpath(card), "title_xpath": tree.getroottree().getpath(heading),
                "support_urls": []})
    for scope in output:
        scope["offering_key"] = seed["start_url"] + "::" + sha256(programme_key(scope["name"]).encode()).hexdigest()[:16]
    keys = [scope["offering_key"] for scope in output]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate programme cards require identity review; no scheme was merged silently.")
    return output


def scoped_blocks(blocks, scope):
    if not scope:
        return blocks
    prefix = scope["card_xpath"]
    return [block for block in blocks if block.get("source_xpath", "") == prefix
            or block.get("source_xpath", "").startswith(prefix + "/")]


def section_role(text):
    """Recognize section labels, never classify arbitrary paragraphs by keywords."""
    name = re.sub(r"^\s*Q\.?\s*\d+\s*[:.)]?\s*", "", text, flags=re.I)
    name = re.sub(r"^\s*(?:\d+(?:\.\d+)*[.)]?|\([ivx]+\)|[ivx]+\.)\s*", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip().rstrip(":.?").strip().casefold()
    patterns = {
        "eligibility": r"(?:academic |financial |score and financial )?eligib(?:ility|iltiy)(?: (?:criteri(?:a|on)|conditions|requirements|for (?:the )?scholarships?))?|financial criteria|who (?:can|is eligible to) apply(?: for .*)?|reservation|terms and conditions|eligible students|conditions of eligibility|forfeiture of scholarship|other scholarships",
        "benefits": r"benefits|scholarship benefits|(?:scholarship |award )?amount(?: and number| of scholarship)?|(?:rate|rates|quantum|value|amount) of (?:the )?scholarships?|financial assistance|what (?:does|do) (?:a |the )?chevening scholarships? cover|what is included|what is the (?:amount|rate|value) of (?:pg )?scholarship",
        "documents": r"(?:required |mandatory |supporting )?documents(?: required)?|list of mandatory documents|document checklist|what are the different documents to be uploaded with the application form",
        "selection_process": r"selection(?: criteri(?:a|on)| process| procedure| of scholars| of students)?|procedure for selection|criteria of selection|methods of resolving ties|what is the procedure of selection(?: under .*)?|does reservation system is followed.*|how many slots are reserved for women candidates",
        "renewal_requirements": r"renewals?|procedure for renewal of scholarship(?: under .*)?|renewal(?: requirements| criteria| of scholarship)?|continuation(?: criteri(?:a|on)(?: and duration)?)?|duration(?: of scholarship)?|which students can apply for renewal(?: in .*)?",
        "application_instructions": r"(?:how to apply|application (?:instructions|process|procedure)|procedure for application|procedure for applying)",
        "cycle": r"(?:application|scholarship|academic) (?:cycle|year)",
        "opening_date": r"(?:application )?(?:opening date|start date)|applications open|scheme open from",
        "closing_date": r"(?:application )?(?:closing date|deadline)|deadline|last date(?: to apply| of application)?|student application open till",
    }
    return next((role for role, pattern in patterns.items() if re.fullmatch(pattern, name)), None)


def source_sections(blocks):
    sections = {}
    active = None
    depth = 0
    for block in blocks:
        role = section_role(block["text"])
        if role:
            active = role
            depth = len(block.get("heading_path", []))
        elif block["kind"] != "heading" and inline_role(block["text"]):
            inline = inline_role(block["text"])
            sections.setdefault(inline, []).append(block)
            if active and active != inline and not (active == "benefits" and inline == "documents"):
                sections.setdefault(active, []).append(block)
        elif block["kind"] == "heading":
            if active and len(block.get("heading_path", [])) > depth:
                sections.setdefault(active, []).append(block)
            else:
                active = None
        elif active and block["text"].strip():
            sections.setdefault(active, []).append(block)
    return sections


def inline_role(text):
    """Explicit facts in prose outside labelled sections; no guessed checklist."""
    text = re.sub(r"\s+", " ", text)
    if re.search(r"(?:upload|submit|attach).{0,140}(?:documents?|proof)|(?:documents?|proof).{0,100}(?:uploaded|required)", text, re.I) and re.search(r"scholarship\s+amount\s+(?:more\s+than|less\s+than|above|below|exceed)", text, re.I):
        return "documents"
    money = re.search(r"\b(?:scholarships?|financial assistance)\b.{0,100}(?:Rs\.?|INR|₹)\s*\d", text, re.I)
    income_context = money and re.search(r"\b(?:income|earnings|salary)\b", text[max(0, money.start()-100):money.end()], re.I)
    direct_payment = re.search(r"\b(?:scholarships?\s+(?:of|worth|is|amount(?:\s+is)?)|rate\s+of\s+(?:the\s+)?scholarship|financial\s+assistance\s+(?:of|is))\s*[:,\-]?\s*(?:Rs\.?|INR|₹)\s*\d", text, re.I)
    if money and (not income_context or direct_payment) and re.search(r"per\s+(?:month|annum|year)|monthly|annually", text, re.I):
        return "benefits"
    if not re.search(r"\bno (?:documentary |supporting )?(?:proof|documents?)", text, re.I) and re.search(r"(?:upload|submit|enclose|provide|attach).{0,100}(?:certificate|document|proof)|(?:certificate|document|proof).{0,100}(?:uploaded|submitted|enclosed|required|attached)", text, re.I):
        return "documents"
    if re.search(r"(?:selection|slots|short.?list|tie).{0,100}(?:merit|reserved|marks|basis)|slots.{0,100}(?:reservation|women)", text, re.I):
        return "selection_process"
    if re.search(r"scholarship\s+is\s+open\s+to\s+those\s+students", text, re.I):
        return "eligibility"
    return None
