# Official Microsoft Playwright image with all browser binaries & OS dependencies pre-installed
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

# Prevent python from buffering stdout/stderr
ENV PYTHONUNBUFFERED=1
ENV HEADLESS=true
ENV PORT=8000

WORKDIR /app

# Copy dependency definition
COPY requirements.txt .

# Install dependencies including fastapi and uvicorn
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application code
COPY . .

# Expose web server port
EXPOSE 8000

# Start FastAPI web server on 0.0.0.0
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
