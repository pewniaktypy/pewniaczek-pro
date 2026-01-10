FROM python:3.9-slim

WORKDIR /app

# Instalacja zależności systemowych (jeśli potrzebne dla Pandas/Numpy)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopiujemy wszystko do głównego katalogu (struktura płaska)
COPY . .

EXPOSE 5000

CMD ["python", "main.py"]
