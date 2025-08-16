# Use Python 3.11 slim image as base
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the script
COPY nerdaxe_hashrate_benchmark.py .

# Set the entrypoint
ENTRYPOINT ["python", "-u", "nerdaxe_hashrate_benchmark.py"]
