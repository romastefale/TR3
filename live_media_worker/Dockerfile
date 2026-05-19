FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

ENV PYTHONUNBUFFERED=1
ENV PORT=8080

CMD ["python", "start.py"]
