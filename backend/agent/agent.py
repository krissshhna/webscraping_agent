import os
import re
import json
import time
from dotenv import load_dotenv

import groq
from groq import Groq

from agent.tools import scrape_url
from agent.prompts import SYSTEM_PROMPT, EXTRACTION_PROMPT_TEMPLATE
from agent.schema import ProductRecord

load_dotenv()
load_dotenv(".env")
load_dotenv("env")


def _get_api_key() -> str:
    """Load and validate the Groq API key from environment."""
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key or key == "your_groq_api_key_here":
        raise EnvironmentError(
            "\n[ERROR] GROQ_API_KEY is not set.\n"
            "  1. Copy env.example to .env (or update the 'env' file)\n"
            "  2. Replace 'your_groq_api_key_here' with your real Groq key\n"
            "  Get a key at: https://console.groq.com/keys\n"
        )
    return key


def _get_model_name() -> str:
    """Load the Groq model name from environment, with a sensible default."""
    return os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"


def run_extraction(url: str, verbose: bool = False) -> dict:
    """
    Main agent pipeline:
      1. Scrape the given URL
      2. Send cleaned text to Groq with the extraction system prompt
      3. Parse and validate the response as a ProductRecord
      4. Return a plain dict with the alias (column header) keys

    Args:
        url:     The documentation URL to process.
        verbose: If True, print intermediate steps to stdout.

    Returns:
        A dict matching the ProductRecord schema:
        RawProduct, Vendor, Product, Edition, Version, LicenseMetric.
    """
    # ── 1. Configure Groq client ─────────────────────────────────────────────
    api_key = _get_api_key()
    model_name = _get_model_name()
    client = Groq(api_key=api_key)

    # ── 2. Scrape the URL ────────────────────────────────────────────────────
    if verbose:
        print(f"[agent] Scraping: {url}")

    scrape_result = scrape_url(url)

    if not scrape_result["success"]:
        error_msg = scrape_result["error"]
        raise RuntimeError(f"Scraping failed: {error_msg}")
    else:
        content = scrape_result["content"]
        resolved_url = scrape_result.get("resolved_url", url)

    if verbose:
        if resolved_url != url:
            print(f"[agent] No lifecycle data at original URL — using lifecycle page: {resolved_url}")
        print(f"[agent] Scraped {len(content)} characters of content.")

    # ── 3. Build the extraction prompt ───────────────────────────────────────
    user_prompt = EXTRACTION_PROMPT_TEMPLATE.format(url=url, content=content)

    # ── 4. Call Groq with Retry/Backoff ──────────────────────────────────────
    if verbose:
        print(f"[agent] Sending to Groq for extraction using {model_name}...")

    max_retries = 5
    base_delay = 5
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,           # deterministic output
                response_format={"type": "json_object"},
            )
            break
        except groq.APIStatusError as e:
            if e.status_code == 429 and attempt < max_retries - 1:
                retry_after = 2.0
                try:
                    if hasattr(e, "response") and e.response and hasattr(e.response, "headers"):
                        headers = e.response.headers
                        if "retry-after" in headers:
                            retry_after = float(headers["retry-after"])
                        elif "x-ratelimit-reset-tokens" in headers:
                            val = str(headers["x-ratelimit-reset-tokens"]).strip().lower()
                            # Correctly parse units (ms vs s) to prevent sleeping for minutes on ms reset header
                            if val.endswith("ms"):
                                retry_after = float(val[:-2]) / 1000.0 + 0.1
                            elif val.endswith("s"):
                                retry_after = float(val[:-1]) + 0.1
                            else:
                                match = re.search(r"(\d+(\.\d+)?)\s*(ms|s)?", val)
                                if match:
                                    num = float(match.group(1))
                                    unit = match.group(3)
                                    if unit == "ms":
                                        retry_after = num / 1000.0 + 0.1
                                    else:
                                        retry_after = num + 0.1
                except Exception:
                    pass
                
                # Cap the maximum sleep time to 15 seconds to prevent freezing threads indefinitely
                sleep_time = min(max(retry_after, base_delay * (2 ** attempt)), 15.0)
                print(f"[agent] Rate limited. Waiting {sleep_time:.2f}s before retry (attempt {attempt+1}/{max_retries})...")
                time.sleep(sleep_time)
                continue
            
            if e.status_code == 429:
                raise RuntimeError(
                    "\n[ERROR] Groq API Quota/Rate Limit Exceeded\n"
                    f"  The configured model ({model_name}) has no quota/rate limit available for this API key.\n"
                    "  Options:\n"
                    "    1. Put a different GROQ_API_KEY in env\n"
                    "    2. Set GROQ_MODEL in env to a model with available quota\n"
                    "    3. Wait until quota resets\n"
                )
            raise RuntimeError(f"[ERROR] Groq API error: {e}")
        except Exception as e:
            raise RuntimeError(f"[ERROR] Connection or other error during Groq API call: {e}")

    raw_text = response.choices[0].message.content.strip()

    # Strip accidental markdown code fences if the model adds them
    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```(?:json)?\n?", "", raw_text)
        raw_text = re.sub(r"\n?```$", "", raw_text)

    if verbose:
        print(f"[agent] Raw response received ({len(raw_text)} chars).")

    # ── 5. Parse and validate via Pydantic ───────────────────────────────────
    try:
        parsed_dict = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"[agent] Groq returned invalid JSON:\n{raw_text}\n\nError: {e}")

    # Validate against the Pydantic schema
    record = ProductRecord.model_validate(parsed_dict)

    # Return as a plain dict with the alias (column header) keys
    return record.model_dump(by_alias=True)
