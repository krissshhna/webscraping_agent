import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import re

# Maximum characters of page text to pass to the LLM to stay within token limits
MAX_CONTENT_CHARS = 30_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Signals that a page is a proper lifecycle page with version/edition data
LIFECYCLE_SIGNALS = [
    "mainstream end date",
    "extended end date",
    "lifecycle policy",
    "retirement date",
    "support dates",
    "releases\nversion",
    "editions\n",
]

# Microsoft Lifecycle search endpoint
MS_LIFECYCLE_SEARCH = "https://learn.microsoft.com/api/lifecycle/search/results"


def _has_lifecycle_data(text: str) -> bool:
    """Return True if the scraped text looks like a product lifecycle page."""
    lower = text.lower()
    return any(signal in lower for signal in LIFECYCLE_SIGNALS)


def _find_microsoft_lifecycle_url(product_name: str) -> str | None:
    """
    Construct URL slug from product name directly and check if it exists.
    Saves time by avoiding the deprecated Microsoft search API.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", product_name.lower()).strip("-")
    candidate = f"https://learn.microsoft.com/en-us/lifecycle/products/{slug}"
    try:
        # Check if the URL exists with a short 2-second timeout
        r = requests.head(candidate, headers=HEADERS, timeout=2.0, allow_redirects=True)
        if r.status_code == 200:
            return candidate
    except Exception:
        pass

    return None


def _format_table(table_tag) -> str:
    """Format an HTML table as a clean readable markdown-like text block for the LLM."""
    rows = []
    for tr in table_tag.find_all("tr"):
        cells = [re.sub(r'\s+', ' ', cell.get_text(strip=True)) for cell in tr.find_all(["td", "th"])]
        if cells:
            rows.append(" | ".join(cells))
    if not rows:
        return ""
    return "\n\n[Table Start]\n" + "\n".join(rows) + "\n[Table End]\n\n"


def _fetch_and_clean(url: str) -> dict:
    """
    Fetch a URL and return cleaned text content.
    Returns dict with success, content, error, resolved_url.
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()

        try:
            soup = BeautifulSoup(response.text, "lxml")
        except Exception:
            soup = BeautifulSoup(response.text, "html.parser")

        # ── Extract structured metadata BEFORE removing tags ──

        # Page title
        page_title = ""
        title_tag = soup.find("title")
        if title_tag:
            page_title = title_tag.get_text(strip=True)

        # Meta description
        meta_desc = ""
        desc_tag = soup.find("meta", attrs={"name": "description"})
        if desc_tag:
            meta_desc = desc_tag.get("content", "")

        # Open Graph / Twitter meta tags
        og_data = {}
        for tag in soup.find_all("meta"):
            prop = tag.get("property", tag.get("name", "")).lower()
            content_val = (tag.get("content") or "").strip()
            if content_val and prop in ("og:title", "og:site_name", "og:description",
                                         "twitter:title", "twitter:description"):
                og_data[prop] = content_val

        # JSON-LD structured data
        json_ld_info = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json
                payload = json.loads(script.string or "")
                items = payload if isinstance(payload, list) else [payload]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    parts = []
                    for key in ("name", "brand", "publisher", "version",
                                "applicationCategory", "operatingSystem",
                                "license", "softwareVersion", "@type"):
                        val = item.get(key)
                        if val:
                            if isinstance(val, dict):
                                val = val.get("name", str(val))
                            parts.append(f"{key}: {val}")
                    if item.get("offers"):
                        offers = item["offers"]
                        if isinstance(offers, dict):
                            offers = [offers]
                        for o in (offers or []):
                            if isinstance(o, dict):
                                for ok in ("description", "price", "priceCurrency"):
                                    if o.get(ok):
                                        parts.append(f"offer_{ok}: {o[ok]}")
                    if parts:
                        json_ld_info.append("; ".join(parts))
            except Exception:
                continue

        # Copyright / footer text (often has vendor name)
        copyright_text = ""
        for tag in soup.find_all(string=re.compile(r'©|copyright|\(c\)', re.IGNORECASE)):
            text = tag.strip()
            if text and len(text) < 200:
                copyright_text = text
                break

        # Format tables to preserve their structure before extracting raw text
        for table in soup.find_all("table"):
            table_text = _format_table(table)
            if table_text:
                table.replace_with(soup.new_string(table_text))

        # Format definition lists (<dl>) which many product pages use
        for dl in soup.find_all("dl"):
            parts = []
            for child in dl.children:
                if hasattr(child, 'name'):
                    if child.name == "dt":
                        parts.append(f"\n{child.get_text(strip=True)}: ")
                    elif child.name == "dd":
                        parts.append(child.get_text(strip=True))
            if parts:
                dl.replace_with(soup.new_string("".join(parts)))

        # Remove noise elements
        for tag in soup(["script", "style", "nav", "noscript", "iframe", "svg",
                          "button", "meta", "link"]):
            tag.decompose()

        # Extract content from body (fall back to the whole page if body not found)
        content_root = soup.body or soup
        raw_text = content_root.get_text(separator="\n")

        # Collapse excessive whitespace / blank lines
        lines = [line.strip() for line in raw_text.splitlines()]
        cleaned = "\n".join(line for line in lines if line)

        # ── Build enriched header context ──
        header_context = ""
        if page_title:
            header_context += f"Page Title: {page_title}\n"
        if meta_desc:
            header_context += f"Page Description: {meta_desc}\n"
        for key, val in og_data.items():
            header_context += f"Meta {key}: {val}\n"
        if json_ld_info:
            header_context += "Structured Data: " + " | ".join(json_ld_info) + "\n"
        if copyright_text:
            header_context += f"Copyright: {copyright_text}\n"
        if header_context:
            cleaned = header_context + "\n" + cleaned

        # Truncate to avoid exceeding LLM context window
        if len(cleaned) > MAX_CONTENT_CHARS:
            cleaned = cleaned[:MAX_CONTENT_CHARS] + "\n\n[... content truncated ...]"

        return {"success": True, "content": cleaned, "error": "", "resolved_url": url}

    except requests.exceptions.Timeout:
        return {"success": False, "content": "", "error": f"Request timed out for URL: {url}", "resolved_url": url}
    except requests.exceptions.HTTPError as e:
        return {"success": False, "content": "", "error": f"HTTP error {e.response.status_code}: {e}", "resolved_url": url}
    except requests.exceptions.RequestException as e:
        return {"success": False, "content": "", "error": f"Request failed: {e}", "resolved_url": url}
    except Exception as e:
        return {"success": False, "content": "", "error": f"Unexpected error: {e}", "resolved_url": url}


def _extract_product_name_from_content(content: str) -> str:
    """
    Extract the most likely product name from scraped content.
    Looks at the first meaningful non-generic heading lines.
    """
    skip_words = {"documentation", "overview", "get started", "what's new",
                  "download", "read in english", "edit", "links"}
    for line in content.splitlines():
        line = line.strip()
        if 3 < len(line) < 80 and line.lower() not in skip_words:
            return line
    return ""


def scrape_url(url: str) -> dict:
    """
    Fetches a web page and returns its cleaned text content.
    Only scrapes the provided URL.

    Args:
        url: The full URL of the page to scrape.

    Returns:
        A dict with keys:
          - "success"      (bool)
          - "content"      (str): Cleaned page text (up to MAX_CONTENT_CHARS chars)
          - "error"        (str): Error message if success is False
          - "resolved_url" (str): The final URL that was actually scraped
    """
    # Validate URL scheme
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"success": False, "content": "", "error": "Invalid URL scheme. Must be http or https.", "resolved_url": url}

    # Scrape only the given URL
    return _fetch_and_clean(url)
