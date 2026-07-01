"""
file_parser.py — Parse uploaded .txt, .csv, or .xlsx files to extract URLs.
"""

import io
import re
from typing import Union

import pandas as pd

URL_PATTERN = re.compile(
    r'https?://[^\s\'"<>]+',
    re.IGNORECASE
)


def _looks_like_url(value: str) -> bool:
    """Return True if value resembles a URL."""
    value = str(value).strip()
    return value.startswith(("http://", "https://"))


def _extract_urls_from_text(text: str) -> list[str]:
    """Extract URLs from plain text (one per line or via regex)."""
    urls = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if _looks_like_url(line):
            urls.append(line)
        else:
            # Try regex in case there's surrounding whitespace/quotes
            found = URL_PATTERN.findall(line)
            urls.extend(found)
    return urls


def _find_url_columns(df: pd.DataFrame) -> list[str]:
    """Find columns in a DataFrame that contain URL-like values."""
    url_columns = []
    for col in df.columns:
        sample = df[col].dropna().head(10)
        url_count = sum(1 for v in sample if _looks_like_url(str(v)))
        if url_count > 0:
            url_columns.append(col)
    # Prefer columns named 'url', 'link', 'href', etc.
    priority = [c for c in url_columns
                if any(kw in str(c).lower() for kw in ("url", "link", "href", "address", "site"))]
    return priority or url_columns


def parse_uploaded_file(filename: str, content: bytes) -> tuple[list[str], str | None]:
    """
    Parse an uploaded file and return (list_of_urls, error_message).
    Supports .txt, .csv, .xlsx, .xls files.
    """
    filename_lower = filename.lower()

    try:
        if filename_lower.endswith(".txt"):
            text = content.decode("utf-8", errors="replace")
            urls = _extract_urls_from_text(text)
            if not urls:
                return [], "No valid URLs found in the text file."
            return urls, None

        elif filename_lower.endswith(".csv"):
            # Try reading as CSV
            text = content.decode("utf-8", errors="replace")
            # Check if it's plain text (no commas → treat as text file)
            if "," not in text and "\t" not in text:
                urls = _extract_urls_from_text(text)
                return urls, None if urls else "No valid URLs found."

            df = pd.read_csv(io.StringIO(text), dtype=str)
            url_cols = _find_url_columns(df)
            if not url_cols:
                # Fall back to full text search
                urls = _extract_urls_from_text(text)
                return urls, None if urls else "No URL columns found in CSV."
            urls = []
            for col in url_cols:
                for val in df[col].dropna():
                    v = str(val).strip()
                    if _looks_like_url(v):
                        urls.append(v)
            return list(dict.fromkeys(urls)), None  # deduplicate preserving order

        elif filename_lower.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(content), dtype=str, sheet_name=0)
            url_cols = _find_url_columns(df)
            if not url_cols:
                # Try extracting from all text
                all_text = df.to_string()
                urls = URL_PATTERN.findall(all_text)
                return list(dict.fromkeys(urls)), None if urls else "No URL columns found in Excel file."
            urls = []
            for col in url_cols:
                for val in df[col].dropna():
                    v = str(val).strip()
                    if _looks_like_url(v):
                        urls.append(v)
            return list(dict.fromkeys(urls)), None

        else:
            # Try as plain text
            try:
                text = content.decode("utf-8", errors="replace")
                urls = _extract_urls_from_text(text)
                return urls, None if urls else f"Unsupported file type: {filename}"
            except Exception:
                return [], f"Unsupported file type: {filename}"

    except Exception as e:
        return [], f"Failed to parse file '{filename}': {str(e)}"
