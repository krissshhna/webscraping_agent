@echo off
echo   ======================================
echo     WebScrape Agent - Starting Up
echo   ======================================

echo Checking Python virtual environment...
if not exist "venv\Scripts\python.exe" (
    echo [!] Virtual environment not found. Please create one and install dependencies.
    pause
    exit /b
)

echo Starting server on http://localhost:8000
echo Open your browser at: http://localhost:8000
echo Press Ctrl+C to stop

cd backend
..\venv\Scripts\python.exe -m uvicorn main:app --host localhost --port 8000 --reload
