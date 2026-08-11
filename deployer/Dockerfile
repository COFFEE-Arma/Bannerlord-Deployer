FROM python:3.12-slim

# p7zip extracts the .7z archives ModDB serves for this mod
RUN apt-get update \
    && apt-get install -y --no-install-recommends p7zip-full \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

CMD ["python", "bot.py"]
