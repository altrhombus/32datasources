# Dockerfile for 32datasources web scraper
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose port 8081 (for future web server, if needed)
EXPOSE 8081

# Default: run the scraper
CMD ["python", "scrape_auction.py"]
