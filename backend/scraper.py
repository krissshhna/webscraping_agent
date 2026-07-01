"""
scraper.py — Async web scraping logic
Uses httpx for concurrent HTTP requests and BeautifulSoup + lxml for parsing.
Extracts: Vendor, Product, Edition, Version, Licence Metric
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from agent.agent import run_extraction

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ScrapedResult:
    url: str
    vendor: str = ""
    product: str = ""
    edition: str = ""
    version: str = ""
    licence_metric: str = ""
    eos: str = "N/A"
    eol: str = "N/A"
    status: str = "success"

@dataclass
class ErrorResult:
    url: str
    error: str
    status: str = "error"


# ---------------------------------------------------------------------------
# Constants & patterns
# ---------------------------------------------------------------------------

VERSION_PATTERN = re.compile(
    r'\bv?(\d+\.\d+(?:\.\d+)*(?:[-._]\w+)?)\b', re.IGNORECASE
)

EDITION_KEYWORDS = [
    "Enterprise", "Professional", "Pro", "Standard", "Business",
    "Ultimate", "Premier", "Community", "Essential", "Advanced",
    "Basic", "Free", "Premium", "Corporate", "Developer", "Team",
    "Starter", "Growth", "Scale", "Plus", "Lite", "Express",
]

LICENCE_KEYWORDS = {
    "per seat": ["per seat", "per-seat", "seat-based", "per workstation"],
    "per user": ["per user", "per-user", "user-based", "named user", "per named user"],
    "per core": ["per core", "per-core", "core-based", "per processor", "per cpu"],
    "per device": ["per device", "per-device", "device-based"],
    "subscription": ["subscription", "monthly", "annually", "annual plan", "per month", "per year"],
    "perpetual": ["perpetual", "one-time", "one time purchase", "lifetime license", "lifetime licence"],
    "open source": ["open source", "open-source", "mit license", "apache license", "gpl", "bsd license"],
    "freemium": ["freemium", "free tier", "free plan", "free forever", "free version"],
    "usage-based": ["usage-based", "pay as you go", "pay-as-you-go", "metered", "consumption-based"],
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

TIMEOUT = httpx.Timeout(15.0, connect=8.0)
MAX_CONCURRENT = 20
DOMAIN_DELAY = 0.5  # seconds between requests to same domain


# ---------------------------------------------------------------------------
# Domain-rate-limit semaphore tracker
# ---------------------------------------------------------------------------

_domain_locks: dict[str, asyncio.Lock] = {}
_domain_last_request: dict[str, float] = {}


def _get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return url


async def _domain_throttle(domain: str):
    """Enforce per-domain rate limiting."""
    if domain not in _domain_locks:
        _domain_locks[domain] = asyncio.Lock()
    async with _domain_locks[domain]:
        last = _domain_last_request.get(domain, 0)
        elapsed = time.monotonic() - last
        if elapsed < DOMAIN_DELAY:
            await asyncio.sleep(DOMAIN_DELAY - elapsed)
        _domain_last_request[domain] = time.monotonic()


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _extract_from_json_ld(soup: BeautifulSoup) -> dict:
    """Extract product fields from JSON-LD structured data."""
    data = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "")
            # Handle array or single object
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                if not isinstance(item, dict):
                    continue
                schema_type = item.get("@type", "")
                if isinstance(schema_type, list):
                    schema_type = " ".join(schema_type)
                relevant_types = {"Product", "SoftwareApplication", "WebApplication",
                                  "SoftwareSourceCode", "Service", "CreativeWork"}
                if any(t in schema_type for t in relevant_types) or not data:
                    if item.get("name") and not data.get("product"):
                        data["product"] = item["name"]
                    if item.get("brand"):
                        brand = item["brand"]
                        data["vendor"] = brand.get("name", brand) if isinstance(brand, dict) else brand
                    if item.get("publisher"):
                        pub = item["publisher"]
                        if not data.get("vendor"):
                            data["vendor"] = pub.get("name", pub) if isinstance(pub, dict) else pub
                    if item.get("version") and not data.get("version"):
                        data["version"] = str(item["version"])
                    if item.get("offers"):
                        offers = item["offers"]
                        if isinstance(offers, dict):
                            offers = [offers]
                        for offer in (offers or []):
                            if isinstance(offer, dict):
                                desc = offer.get("description", "")
                                if desc and not data.get("licence_metric"):
                                    data["licence_metric"] = _match_licence(desc)
        except Exception:
            continue
    return data


def _extract_meta(soup: BeautifulSoup) -> dict:
    """Extract from Open Graph, Twitter Card, and standard meta tags."""
    data = {}
    meta_map = {
        "og:title": "product",
        "og:site_name": "vendor",
        "og:description": "description",
        "twitter:title": "product",
        "twitter:site": "vendor",
        "application-name": "product",
    }
    for tag in soup.find_all("meta"):
        prop = tag.get("property", tag.get("name", "")).lower()
        content = (tag.get("content") or "").strip()
        if not content:
            continue
        key = meta_map.get(prop)
        if key and not data.get(key):
            data[key] = content
    return data


def _extract_version(text: str) -> str:
    """Find version string in text using regex."""
    match = VERSION_PATTERN.search(text)
    return match.group(1) if match else ""


def _extract_edition(text: str) -> str:
    """Find edition keyword in text."""
    text_lower = text.lower()
    for kw in EDITION_KEYWORDS:
        pattern = re.compile(rf'\b{re.escape(kw)}\b', re.IGNORECASE)
        if pattern.search(text_lower):
            return kw
    return ""


def _match_licence(text: str) -> str:
    """Match licence metric keywords in text."""
    text_lower = text.lower()
    for metric, patterns in LICENCE_KEYWORDS.items():
        for p in patterns:
            if p in text_lower:
                return metric.title()
    return ""


def _extract_vendor_from_domain(url: str) -> str:
    """Fallback: extract vendor name from domain."""
    try:
        domain = urlparse(url).netloc.lower()
        domain = re.sub(r'^www\.', '', domain)
        domain = domain.split('.')[0]
        return domain.title()
    except Exception:
        return ""


def _clean(text: str) -> str:
    """Strip and truncate extracted text."""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:200]


# ---------------------------------------------------------------------------
# Core scrape function
# ---------------------------------------------------------------------------

async def scrape_url(url: str, client: httpx.AsyncClient) -> ScrapedResult | ErrorResult:
    """Scrape a single URL and extract product/licence fields."""
    # Basic URL validation
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return ErrorResult(url=url, error="Invalid URL: missing scheme or domain")
    except Exception:
        return ErrorResult(url=url, error="Invalid URL format")

    domain = _get_domain(url)
    await _domain_throttle(domain)

    try:
        response = await client.get(url, follow_redirects=True, timeout=TIMEOUT)

        if response.status_code == 404:
            return ErrorResult(url=url, error=f"HTTP 404 Not Found")
        elif response.status_code == 403:
            return ErrorResult(url=url, error=f"HTTP 403 Forbidden")
        elif response.status_code == 429:
            return ErrorResult(url=url, error=f"HTTP 429 Too Many Requests")
        elif response.status_code >= 500:
            return ErrorResult(url=url, error=f"HTTP {response.status_code} Server Error")
        elif response.status_code >= 400:
            return ErrorResult(url=url, error=f"HTTP {response.status_code} Client Error")

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            # Non-HTML content — try to extract version from URL/content-disposition
            return ScrapedResult(
                url=url,
                vendor=_extract_vendor_from_domain(url),
                product=url.split("/")[-1] or url,
                version=_extract_version(url),
                status="success",
            )

        html = response.text
        soup = BeautifulSoup(html, "lxml")

        # --- Remove noise ---
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        full_text = soup.get_text(separator=" ", strip=True)

        # --- Cascade extraction ---
        json_ld = _extract_from_json_ld(soup)
        meta = _extract_meta(soup)

        title_tag = soup.find("title")
        page_title = _clean(title_tag.get_text() if title_tag else "")

        h1_tag = soup.find("h1")
        h1_text = _clean(h1_tag.get_text() if h1_tag else "")

        # PRODUCT
        product = (
            _clean(json_ld.get("product", ""))
            or _clean(meta.get("product", ""))
            or h1_text
            or page_title.split("|")[0].split("–")[0].split("-")[0].strip()
            or ""
        )

        # VENDOR
        vendor = (
            _clean(json_ld.get("vendor", ""))
            or _clean(meta.get("vendor", ""))
            or _extract_vendor_from_domain(url)
        )

        # VERSION — search in title, h1, full text
        version = (
            json_ld.get("version", "")
            or _extract_version(page_title)
            or _extract_version(h1_text)
            or _extract_version(full_text[:3000])
        )

        # EDITION — search title, h1, meta description, body text
        combined_text = f"{page_title} {h1_text} {meta.get('description', '')} {full_text[:5000]}"
        edition = _extract_edition(combined_text)

        # LICENCE METRIC — search broader text
        licence_text = f"{meta.get('description', '')} {full_text[:8000]}"
        licence_metric = (
            _match_licence(json_ld.get("licence_metric", ""))
            or _match_licence(licence_text)
        )

        # EOS / EOL HEURISTICS
        eos = "N/A"
        eol = "N/A"

        # Search for dates or status following support/lifecycle keywords
        eos_pat = re.compile(
            r'(?:end of support|mainstream support|support ends|retirement date)[^\n\d]*(\d{4}[-/.]\d{2}[-/.]\d{2}|\b[A-Za-z]+ \d{1,2}, \d{4}\b)',
            re.IGNORECASE
        )
        eol_pat = re.compile(
            r'(?:end of life|eol|extended support|retirement|retired on)[^\n\d]*(\d{4}[-/.]\d{2}[-/.]\d{2}|\b[A-Za-z]+ \d{1,2}, \d{4}\b)',
            re.IGNORECASE
        )

        eos_match = eos_pat.search(full_text[:8000])
        if eos_match:
            eos = eos_match.group(1).strip()

        eol_match = eol_pat.search(full_text[:8000])
        if eol_match:
            eol = eol_match.group(1).strip()

        return ScrapedResult(
            url=url,
            vendor=_clean(vendor),
            product=_clean(product),
            edition=edition,
            version=_clean(version),
            licence_metric=licence_metric,
            eos=eos,
            eol=eol,
        )

    except httpx.ConnectTimeout:
        return ErrorResult(url=url, error="Connection Timeout")
    except httpx.ReadTimeout:
        return ErrorResult(url=url, error="Read Timeout")
    except httpx.ConnectError as e:
        return ErrorResult(url=url, error=f"Connection Refused / DNS Failure")
    except httpx.TooManyRedirects:
        return ErrorResult(url=url, error="Too Many Redirects")
    except httpx.HTTPStatusError as e:
        return ErrorResult(url=url, error=f"HTTP {e.response.status_code}")
    except httpx.InvalidURL:
        return ErrorResult(url=url, error="Invalid URL")
    except Exception as e:
        return ErrorResult(url=url, error=f"Unexpected error: {str(e)[:120]}")


# ---------------------------------------------------------------------------
# Batch scraper
# ---------------------------------------------------------------------------

async def scrape_url_llm(url: str) -> ScrapedResult | ErrorResult:
    """Run extraction via LLM agent in a worker thread."""
    try:
        data = await asyncio.to_thread(run_extraction, url)
        return ScrapedResult(
            url=url,
            vendor=data.get("Vendor", ""),
            product=data.get("Product", ""),
            edition=data.get("Edition", ""),
            version=data.get("Version", ""),
            licence_metric=data.get("LicenseMetric", ""),
            eos=data.get("EOS", "N/A"),
            eol=data.get("EOL", "N/A"),
            status="success",
        )
    except Exception as e:
        error_msg = str(e)
        if error_msg.startswith("[agent] "):
            error_msg = error_msg[len("[agent] "):]
        elif error_msg.startswith("[ERROR] "):
            error_msg = error_msg[len("[ERROR] "):]
        return ErrorResult(url=url, error=error_msg)


async def scrape_all(urls: list[str]) -> tuple[list[ScrapedResult], list[ErrorResult]]:
    """
    Scrape all URLs concurrently using the Groq LLM agent.
    Limits concurrency to 5 to avoid API rate limits.
    """
    semaphore = asyncio.Semaphore(5)

    async def bounded_scrape(url: str):
        async with semaphore:
            return await scrape_url_llm(url)

    tasks = [bounded_scrape(url.strip()) for url in urls if url.strip()]
    raw = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[ScrapedResult] = []
    errors: list[ErrorResult] = []

    for url, item in zip([u.strip() for u in urls if u.strip()], raw):
        if isinstance(item, Exception):
            errors.append(ErrorResult(url=url, error=str(item)[:150]))
        elif isinstance(item, ErrorResult):
            errors.append(item)
        elif isinstance(item, ScrapedResult):
            results.append(item)

    return results, errors
