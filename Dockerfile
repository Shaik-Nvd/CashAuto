# Official Microsoft Playwright image with latest browser binaries
FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

# Prevent python from buffering stdout/stderr
ENV PYTHONUNBUFFERED=1
ENV HEADLESS=true
ENV PORT=8000

WORKDIR /app

# Copy dependency definition
COPY requirements.txt .

# Install dependencies and update browser binaries
RUN pip install --no-cache-dir -r requirements.txt && \
    python -m playwright install --with-deps chromium

# Copy all application code
COPY . .

# Expose web server port
EXPOSE 8000

# Start FastAPI web server on 0.0.0.0
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
