"""Bounded discovery with snapshots and explicit navigation decisions."""

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import unquote, urldefrag, urlsplit, urlunsplit

import scrapy
from scrapy.http import HtmlResponse

from scholarship_intelligence.settings import (
    MAX_EXTERNAL_PAGES_PER_SEED,
    MAX_PAGES_PER_SEED,
    SCRAPY_SETTINGS,
    SEEDS_PATH,
)
from scholarship_intelligence.snapshots import save_raw_snapshot


RELEVANCE_TERMS = (
    "scholarship",
    "fellowship",
    "financial aid",
    "financial assistance",
    "eligibility",
    "guideline",
    "faq",
    "application",
    "apply",
    "deadline",
    "extension",
    "notice",
    "announcement",
    "renewal",
    "mext",
    "research student",
    "undergraduate",
    "postgraduate",
    "college of technology",
    "specialized training",
    "japanese studies",
)

BLOCKED_EXTERNAL_HOSTS = {
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "instagram.com",
    "youtube.com",
    "wa.me",
    "api.whatsapp.com",
    "play.google.com",
    "apps.apple.com",
    "expertia.ai",
}

IRRELEVANT_PATH_SEGMENTS = {
    "careers",
    "jobs",
    "donate",
    "donation",
    "donations",
    "privacy",
    "privacy-policy",
    "terms",
    "terms-and-conditions",
}

MAX_REDIRECT_HOPS = 5


class ScholarshipSpider(scrapy.Spider):
    name = "scholarship_pilot"

    custom_settings = {
        **SCRAPY_SETTINGS,
        "REDIRECT_ENABLED": False,
        "METAREFRESH_ENABLED": False,
        "FEED_EXPORT_ENCODING": "utf-8",
        "DOWNLOAD_MAXSIZE": 10 * 1024 * 1024,
        # Our navigation_depth check is the single depth policy.
        "SPIDER_MIDDLEWARES": {
            "scrapy.spidermiddlewares.depth.DepthMiddleware": None,
        },
    }

    def __init__(self, *args, seed_id=None, max_pages=MAX_PAGES_PER_SEED, continue_from=None, **kwargs):
        super().__init__(*args, **kwargs)
        selected_seed_id = seed_id
        self.max_pages = int(max_pages)
        if not 1 <= self.max_pages <= 1000:
            raise ValueError("Crawl page budget must be between 1 and 1000.")
        self.continuing = continue_from is not None
        self.frontier = []
        self.previous_fetches = {}

        data = json.loads(SEEDS_PATH.read_text(encoding="utf-8"))
        self.seeds = data["seeds"]
        if not isinstance(self.seeds, list) or not self.seeds:
            raise ValueError("Seeds must be a nonempty list.")

        seed_ids = set()
        for seed in self.seeds:
            seed_id = seed["seed_id"]
            if not isinstance(seed_id, str) or not seed_id:
                raise ValueError("Each seed needs a nonempty string ID.")
            if seed_id in seed_ids:
                raise ValueError(f"Duplicate seed ID: {seed_id}")
            seed_ids.add(seed_id)

            hosts = seed["allowed_hosts"]
            if not isinstance(hosts, list) or not hosts:
                raise ValueError(f"Missing host scope for {seed_id}")
            if not all(isinstance(host, str) and host for host in hosts):
                raise ValueError(f"Invalid host scope for {seed_id}")

            parsed = urlsplit(seed["start_url"])
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname not in hosts
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(f"Invalid starting URL for {seed_id}")

        if selected_seed_id is not None:
            self.seeds = [seed for seed in self.seeds if seed["seed_id"] == selected_seed_id]
            if not self.seeds:
                raise ValueError(f"Unknown seed ID: {selected_seed_id}")

        self.allowed_domains = sorted(
            {host for seed in self.seeds for host in seed["allowed_hosts"]}
        )
        self.scheduled = {seed["seed_id"]: set() for seed in self.seeds}
        self.external_leads = {seed["seed_id"]: set() for seed in self.seeds}

        if self.continuing:
            if selected_seed_id is None:
                raise ValueError("Crawl continuation requires one seed ID.")
            prior = Path(continue_from).resolve()
            runtime = (SEEDS_PATH.resolve().parent.parent / "data/runtime").resolve()
            if not prior.is_relative_to(runtime):
                raise ValueError("Continuation crawl report must be inside runtime storage.")
            events = [json.loads(line) for line in prior.read_text(encoding="utf-8").splitlines() if line.strip()]
            events = [row for row in events if row.get("seed_id") == selected_seed_id]
            depths = {}
            for row in events:
                if row.get("event") == "fetched" and isinstance(row.get("snapshot"), dict):
                    url = row["snapshot"]["source_url"]
                    self.previous_fetches[url] = row.get("http_status")
                    depths[url] = row.get("navigation_depth", 0)
                elif row.get("event") in {"request_failed", "snapshot_failed"} and isinstance(row.get("url"), str):
                    self.previous_fetches[row["url"]] = None
            frontier = {}
            for row in events:
                if row.get("event") not in {"discovery", "redirect"} or row.get("decision") != "page_limit":
                    continue
                url = row.get("scheduled_url") or row.get("url")
                if not isinstance(url, str) or url in self.previous_fetches:
                    continue
                parent = row.get("parent_url")
                depth = row.get("navigation_depth", depths.get(parent, 0) + (row.get("event") != "redirect"))
                frontier.setdefault(url, (url, parent, depth, row.get("link_text", "")))
            seed = self.seeds[0]
            self.frontier = sorted(frontier.values(), key=lambda row: -self.link_priority(seed, row[0], row[3]))

    @staticmethod
    def normalize_navigation_url(seed, url):
        """Remove fragments and apply only a demonstrated hostname alias."""
        url = urldefrag(url)[0]
        parsed = urlsplit(url)

        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Invalid navigation URL.")

        # Validate any explicit port before constructing a request.
        port = parsed.port

        if (
            seed["seed_id"] == "chevening_india"
            and parsed.hostname == "chevening.org"
            and port in {None, 80, 443}
        ):
            netloc = "www.chevening.org"
            if port is not None:
                netloc += f":{port}"
            url = urlunsplit(parsed._replace(netloc=netloc))

        return url

    def request_for(
        self,
        seed,
        url,
        parent_url,
        navigation_depth,
        *,
        redirect_hops=0,
    ):
        """Return a scoped request or a scheduling rejection reason."""
        try:
            url = self.normalize_navigation_url(seed, url)
        except ValueError:
            return None, "invalid_url"

        seed_id = seed["seed_id"]
        if urlsplit(url).hostname not in seed["allowed_hosts"]:
            return None, "external_host"

        if redirect_hops > MAX_REDIRECT_HOPS:
            return None, "redirect_limit"

        if navigation_depth > SCRAPY_SETTINGS["DEPTH_LIMIT"]:
            return None, "depth_limit"

        if self.continuing and url in self.previous_fetches:
            return None, "previously_fetched"
        if url in self.scheduled[seed_id]:
            return None, "already_scheduled"

        if len(self.scheduled[seed_id]) >= self.max_pages:
            return None, "page_limit"

        request = scrapy.Request(
            url,
            callback=self.parse,
            errback=self.handle_failure,
            meta={
                "seed": seed,
                "parent_url": parent_url,
                "navigation_depth": navigation_depth,
                "redirect_hops": redirect_hops,
                "handle_httpstatus_all": True,
            },
        )
        self.scheduled[seed_id].add(url)
        return request, "scheduled"

    async def start(self):
        if self.continuing:
            for url, parent, depth, text in self.frontier:
                request, reason = self.request_for(self.seeds[0], url, parent, depth)
                yield {"event": "discovery", "seed_id": self.seeds[0]["seed_id"], "parent_url": parent,
                       "url": url, "scheduled_url": request.url if request is not None else None,
                       "navigation_depth": depth, "link_text": text, "decision": reason,
                       "continuation": True}
                if request is not None:
                    yield request
            return
        for seed in self.seeds:
            request, reason = self.request_for(seed, seed["start_url"], None, 0)
            if request is None:
                raise ValueError(f"Cannot start {seed['seed_id']}: {reason}")
            yield request
            for context_url in seed.get("context_urls", []):
                request, reason = self.request_for(seed, context_url, seed["start_url"], 0)
                yield {"event": "discovery", "seed_id": seed["seed_id"], "parent_url": seed["start_url"],
                       "url": context_url, "decision": reason, "link_text": "Configured official context",
                       "navigation_depth": 0, "scheduled_url": request.url if request is not None else None}
                if request is not None:
                    request.meta["context_only"] = True
                    yield request

    @staticmethod
    def link_priority(seed, url, text):
        """Return a navigation priority; zero means irrelevant."""
        try:
            parsed = urlsplit(url)
            parsed.port
        except ValueError:
            return 0

        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return 0

        host = parsed.hostname.lower()
        if any(
            host == blocked or host.endswith("." + blocked)
            for blocked in BLOCKED_EXTERNAL_HOSTS
        ):
            return 0

        path = unquote(parsed.path).lower()
        segments = [segment for segment in path.split("/") if segment]
        if any(segment in IRRELEVANT_PATH_SEGMENTS for segment in segments):
            return 0

        # Avoid NSP operator navigation and clearly operator-specific FAQs.
        if seed["seed_id"] == "nsp":
            if segments and segments[0] in {
                "institute",
                "institutes",
                "institution",
                "institutions",
                "officer",
                "officers",
            }:
                return 0
            if any(
                term in path
                for term in ("dno_sno", "sno_dno", "institute_verification")
            ):
                return 0

        # Country-specific pages other than India are outside this seed's scope.
        if seed["seed_id"] == "chevening_india":
            if (
                len(segments) >= 2
                and segments[0] == "scholarship"
                and segments[1] != "india"
            ):
                return 0

        # Query parameters are excluded: sharing URLs may embed scholarship text.
        context = f"{path} {text.lower()}".replace("_", " ").replace("-", " ")
        if not any(term in context for term in RELEVANCE_TERMS):
            return 0

        if path.endswith(".pdf"):
            return 100
        if any(term in context for term in ("guideline", "eligibility", "faq")):
            return 90
        if any(term in context for term in ("extension", "deadline", "notice", "announcement")):
            return 85
        if "/resource-hub/guidance/" in path:
            return 80
        if any(term in context for term in ("application", "apply")):
            return 75
        if "/scholarships-awards/" in path:
            return 70
        if "/news/" in path or "/blog/" in path:
            return 20
        return 60

    @staticmethod
    def page_role(response):
        content_type = response.headers.get(b"Content-Type", b"").lower()
        if b"application/pdf" in content_type or response.body.startswith(b"%PDF-"):
            return "document"

        if not isinstance(response, HtmlResponse):
            return "unclassified"

        if response.meta["navigation_depth"] == 0:
            return "seed"

        title = " ".join(response.css("title::text").getall())
        context = f"{urlsplit(response.url).path} {title}".lower()
        if any(term in context for term in ("extension", "notice", "announcement")):
            return "announcement"
        if any(term in context for term in ("guideline", "eligibility", "faq")):
            return "guideline"
        if any(term in context for term in ("application", "apply")):
            return "application"
        return "candidate_detail"

    def parse(self, response):
        seed = response.meta["seed"]
        seed_id = seed["seed_id"]
        depth = response.meta["navigation_depth"]
        content_type = response.headers.get(b"Content-Type", b"").decode(
            "latin-1"
        ) or None

        try:
            snapshot = save_raw_snapshot(
                source_url=response.url,
                content=response.body,
                retrieved_at=datetime.now(timezone.utc),
                content_type=content_type,
            )
        except (OSError, ValueError) as error:
            yield {
                "event": "snapshot_failed",
                "seed_id": seed_id,
                "url": response.url,
                "error": str(error)[:300],
            }
            return

        yield {
            "event": "fetched",
            "seed_id": seed_id,
            "source_category": seed["source_category"],
            "parent_url": response.meta["parent_url"],
            "http_status": response.status,
            "page_role": "supporting_document" if response.meta.get("context_only") else self.page_role(response),
            "navigation_depth": depth,
            "snapshot": asdict(snapshot),
        }

        if response.status in {301, 302, 303, 307, 308}:
            location = response.headers.get(b"Location")
            if location:
                try:
                    target = response.urljoin(location.decode("latin-1"))
                    request, reason = self.request_for(
                        seed,
                        target,
                        response.url,
                        depth,
                        redirect_hops=response.meta.get("redirect_hops", 0) + 1,
                    )
                except ValueError:
                    target = location.decode("latin-1")
                    request, reason = None, "invalid_url"

                yield {
                    "event": "redirect",
                    "seed_id": seed_id,
                    "parent_url": response.url,
                    "url": target,
                    "scheduled_url": request.url if request is not None else None,
                    "decision": reason,
                }
                if request is not None:
                    yield request
            return

        if not 200 <= response.status < 300:
            return
        if not isinstance(response, HtmlResponse):
            return
        if response.meta.get("context_only"):
            return

        # Rank links before scheduling so document links get budget first.
        candidates = {}
        for anchor in response.css("a[href]"):
            href = anchor.attrib["href"].strip()
            if not href or href.startswith("#"):
                continue

            try:
                discovered_url = urldefrag(response.urljoin(href))[0]
                navigation_url = self.normalize_navigation_url(
                    seed, discovered_url
                )
            except ValueError:
                continue

            text = " ".join(anchor.xpath(".//text()").getall()).strip()
            priority = self.link_priority(seed, navigation_url, text)
            if priority == 0:
                continue

            previous = candidates.get(navigation_url)
            if previous is None or priority > previous[0]:
                candidates[navigation_url] = (priority, discovered_url, text)

        for navigation_url, (priority, discovered_url, text) in sorted(
            candidates.items(),
            key=lambda item: item[1][0],
            reverse=True,
        ):
            if urlsplit(navigation_url).hostname not in seed["allowed_hosts"]:
                leads = self.external_leads[seed_id]
                if (
                    navigation_url not in leads
                    and len(leads) < MAX_EXTERNAL_PAGES_PER_SEED
                ):
                    leads.add(navigation_url)
                    yield {
                        "event": "external_lead",
                        "seed_id": seed_id,
                        "parent_url": response.url,
                        "url": discovered_url,
                        "link_text": text[:300],
                        "decision": "recorded_not_fetched",
                    }
                continue

            request, reason = self.request_for(
                seed, navigation_url, response.url, depth + 1
            )
            yield {
                "event": "discovery",
                "seed_id": seed_id,
                "parent_url": response.url,
                "url": discovered_url,
                "scheduled_url": request.url if request is not None else None,
                "link_text": text[:300],
                "navigation_depth": depth + 1,
                "priority": priority,
                "decision": reason,
            }
            if request is not None:
                yield request

    def handle_failure(self, failure):
        request = failure.request
        yield {
            "event": "request_failed",
            "seed_id": request.meta["seed"]["seed_id"],
            "url": request.url,
            "parent_url": request.meta["parent_url"],
            "error_type": failure.type.__name__,
            "error": failure.getErrorMessage()[:300],
        }

    def closed(self, reason):
        self.logger.info(
            "Pilot closed: %s. Scheduled URLs per seed: %s",
            reason,
            {seed_id: len(urls) for seed_id, urls in self.scheduled.items()},
        )
