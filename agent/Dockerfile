FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

EXPOSE 3001

CMD ["uvicorn", "nova_agent:app", "--host", "0.0.0.0", "--port", "3001", "--reload"]
