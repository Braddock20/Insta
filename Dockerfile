
FROM python:3.12-slim-bookworm

WORKDIR /app

# Install Linux dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./requirements.txt

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Install Chromium and its dependencies
RUN python -m playwright install --with-deps chromium

# Copy application
COPY . .

# Start application
CMD ["python", "app.py"]
