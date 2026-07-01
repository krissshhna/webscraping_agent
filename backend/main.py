"""
main.py — FastAPI application for the Web Scraping Agent.

Endpoints:
  POST /api/scrape          — scrape a list of URLs
  POST /api/upload          — parse an uploaded file, return extracted URLs
  GET  /api/export/csv      — download last results as CSV
  GET  /api/export/xlsx     — download last results as XLSX
  GET  /                    — serve the frontend index.html
"""

import asyncio
import io
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from scraper import ScrapedResult, ErrorResult, scrape_all
from file_parser import parse_uploaded_file
from exporter import to_csv, to_xlsx, to_xlsx_augmented

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Web Scraping Agent API",
    description="Batch scrape URLs for vendor/product/licence data",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend static files
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


# ---------------------------------------------------------------------------
# In-memory result store (per-process, single session)
# ---------------------------------------------------------------------------

class ResultStore:
    results: list[ScrapedResult] = []
    errors: list[ErrorResult] = []
    last_updated: Optional[str] = None

store = ResultStore()

# ---------------------------------------------------------------------------
# In-memory bulk-scrape session store  {session_id: {df, augmented_df, filename}}
# ---------------------------------------------------------------------------
bulk_sessions: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ScrapeRequest(BaseModel):
    urls: list[str]

class ScrapeResultItem(BaseModel):
    url: str
    vendor: str
    product: str
    edition: str
    version: str
    licence_metric: str
    eos: str = "N/A"
    eol: str = "N/A"
    status: str

class ErrorItem(BaseModel):
    url: str
    error: str
    status: str

class ScrapeResponse(BaseModel):
    total: int
    success_count: int
    error_count: int
    results: list[ScrapeResultItem]
    errors: list[ErrorItem]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve the frontend index.html."""
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return HTMLResponse(
        content=index_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.post("/api/scrape", response_model=ScrapeResponse)
async def scrape_urls(request: ScrapeRequest):
    """
    Scrape a list of URLs concurrently.
    Stores results in memory for subsequent export calls.
    """
    if not request.urls:
        raise HTTPException(status_code=400, detail="No URLs provided")

    # Deduplicate while preserving order to optimize scraping calls
    seen = set()
    unique_urls = []
    for url in request.urls:
        url = url.strip()
        if url and url not in seen:
            seen.add(url)
            unique_urls.append(url)

    if not unique_urls:
        raise HTTPException(status_code=400, detail="No valid URLs after deduplication")

    if len(unique_urls) > 500:
        raise HTTPException(status_code=400, detail="Maximum 500 URLs per batch")

    # Run the scraper on unique URLs
    unique_results, unique_errors = await scrape_all(unique_urls)

    # Create mapping dictionaries
    url_to_result = {r.url: r for r in unique_results}
    url_to_error = {e.url: e for e in unique_errors}

    # Map back to the original order of requested URLs (including duplicates)
    final_results = []
    final_errors = []
    for url in request.urls:
        url = url.strip()
        if not url:
            continue
        if url in url_to_result:
            final_results.append(url_to_result[url])
        elif url in url_to_error:
            final_errors.append(url_to_error[url])

    # Store the mapped results for file exports so the output files match the uploaded list 1:1
    store.results = final_results
    store.errors = final_errors

    return ScrapeResponse(
        total=len(final_results) + len(final_errors),
        success_count=len(final_results),
        error_count=len(final_errors),
        results=[
            ScrapeResultItem(
                url=r.url,
                vendor=r.vendor,
                product=r.product,
                edition=r.edition,
                version=r.version,
                licence_metric=r.licence_metric,
                eos=r.eos,
                eol=r.eol,
                status=r.status,
            )
            for r in final_results
        ],
        errors=[
            ErrorItem(url=e.url, error=e.error, status=e.status)
            for e in final_errors
        ],
    )


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """
    Parse an uploaded .txt/.csv/.xlsx file and return extracted URLs.
    The client should then call /api/scrape with these URLs.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    allowed_extensions = {".txt", ".csv", ".xlsx", ".xls"}
    suffix = Path(file.filename).suffix.lower()
    if suffix not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. Allowed: .txt, .csv, .xlsx"
        )

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:  # 10 MB limit
        raise HTTPException(status_code=400, detail="File too large (max 10 MB)")

    urls, error_msg = parse_uploaded_file(file.filename, content)

    if error_msg and not urls:
        raise HTTPException(status_code=422, detail=error_msg)

    return {
        "filename": file.filename,
        "url_count": len(urls),
        "urls": urls,
        "warning": error_msg,
    }


@app.get("/api/export/csv")
async def export_csv():
    """Download the last scrape results as a CSV file."""
    if not store.results and not store.errors:
        raise HTTPException(status_code=404, detail="No results to export. Run a scrape first.")

    csv_bytes = to_csv(store.results, store.errors)
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=scrape_results.csv",
            "Content-Length": str(len(csv_bytes)),
        },
    )


@app.get("/api/export/xlsx")
async def export_xlsx():
    """Download the last scrape results as an XLSX file."""
    if not store.results and not store.errors:
        raise HTTPException(status_code=404, detail="No results to export. Run a scrape first.")

    xlsx_bytes = to_xlsx(store.results, store.errors)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=scrape_results.xlsx",
            "Content-Length": str(len(xlsx_bytes)),
        },
    )


@app.get("/api/status")
async def status():
    """Health check endpoint."""
    return {
        "status": "ok",
        "results_in_memory": len(store.results),
        "errors_in_memory": len(store.errors),
    }


# ---------------------------------------------------------------------------
# Bulk Excel Scrape endpoints
# ---------------------------------------------------------------------------

class BulkScrapeRequest(BaseModel):
    session_id: str
    url_column: str


@app.post("/api/upload-excel")
async def upload_excel(file: UploadFile = File(...)):
    """
    Parse an uploaded Excel/CSV file and return column names + preview rows
    so the user can pick the URL column.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".xlsx", ".xls", ".csv"}:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. Allowed: .csv, .xlsx, .xls",
        )

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 10 MB)")

    try:
        if suffix == ".csv":
            df = pd.read_csv(io.BytesIO(content), dtype=str)
        else:
            df = pd.read_excel(io.BytesIO(content), dtype=str, sheet_name=0)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Failed to parse file: {str(e)}")

    if df.empty:
        raise HTTPException(status_code=422, detail="File contains no data rows")

    # Store the DataFrame in memory with a session ID
    session_id = str(uuid.uuid4())
    bulk_sessions[session_id] = {
        "df": df,
        "augmented_df": None,
        "filename": file.filename,
    }

    # Return column names + first 5 rows for preview
    preview = df.head(5).fillna("").to_dict(orient="records")
    return {
        "session_id": session_id,
        "filename": file.filename,
        "columns": list(df.columns),
        "row_count": len(df),
        "preview": preview,
    }


@app.post("/api/bulk-scrape")
async def bulk_scrape(request: BulkScrapeRequest):
    """
    Scrape URLs from the specified column and append scraped data
    as new _webscraped columns to the original DataFrame.
    """
    session = bulk_sessions.get(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found. Please re-upload the file.")

    df = session["df"].copy()

    if request.url_column not in df.columns:
        raise HTTPException(
            status_code=400,
            detail=f"Column '{request.url_column}' not found in uploaded file.",
        )

    # Extract URLs from the selected column
    raw_urls = df[request.url_column].fillna("").astype(str).tolist()

    # Build a list of (index, url) pairs for rows that have valid URLs
    url_pairs = []
    for idx, u in enumerate(raw_urls):
        u = u.strip()
        if u.startswith("http://") or u.startswith("https://"):
            url_pairs.append((idx, u))

    if not url_pairs:
        raise HTTPException(
            status_code=400,
            detail="No valid URLs found in the selected column (must start with http:// or https://).",
        )

    if len(url_pairs) > 500:
        raise HTTPException(status_code=400, detail="Maximum 500 URLs per batch")

    # Scrape all valid URLs
    urls_to_scrape = [u for _, u in url_pairs]
    results, errors = await scrape_all(urls_to_scrape)

    # Build lookup maps
    result_map = {r.url: r for r in results}
    error_map = {e.url: e for e in errors}

    # Prepare new columns with empty defaults
    scraped_fields = [
        "product_webscraped", "vendor_webscraped", "licence_metric_webscraped",
        "edition_webscraped", "version_webscraped", "eol_webscraped", "eos_webscraped",
    ]
    for col in scraped_fields:
        df[col] = ""

    # Fill in scraped data row by row
    success_count = 0
    error_count = 0
    for idx, url in url_pairs:
        if url in result_map:
            r = result_map[url]
            df.at[idx, "product_webscraped"] = r.product
            df.at[idx, "vendor_webscraped"] = r.vendor
            df.at[idx, "licence_metric_webscraped"] = r.licence_metric
            df.at[idx, "edition_webscraped"] = r.edition
            df.at[idx, "version_webscraped"] = r.version
            df.at[idx, "eol_webscraped"] = r.eol
            df.at[idx, "eos_webscraped"] = r.eos
            success_count += 1
        elif url in error_map:
            e = error_map[url]
            df.at[idx, "product_webscraped"] = f"ERROR: {e.error}"
            error_count += 1
        else:
            df.at[idx, "product_webscraped"] = "ERROR: No response"
            error_count += 1

    # Store augmented DataFrame for download
    session["augmented_df"] = df

    # Return full augmented data for live preview
    preview_data = df.fillna("").to_dict(orient="records")
    return {
        "session_id": request.session_id,
        "total": len(url_pairs),
        "success_count": success_count,
        "error_count": error_count,
        "columns": list(df.columns),
        "rows": preview_data,
        "row_count": len(df),
    }


@app.get("/api/bulk-scrape/download/{session_id}")
async def download_bulk_result(session_id: str):
    """Download the augmented Excel file with original data + scraped columns."""
    session = bulk_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    if session["augmented_df"] is None:
        raise HTTPException(status_code=400, detail="No scrape results available. Run bulk scrape first.")

    xlsx_bytes = to_xlsx_augmented(session["augmented_df"])

    # Build download filename from original
    orig_name = Path(session["filename"]).stem
    download_name = f"{orig_name}_webscraped.xlsx"

    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename={download_name}",
            "Content-Length": str(len(xlsx_bytes)),
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )

