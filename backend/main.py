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
import os
import sys
from pathlib import Path
from typing import Optional

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
from exporter import to_csv, to_xlsx

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
    return HTMLResponse(content=index_path.read_text(encoding="utf-8"))


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
