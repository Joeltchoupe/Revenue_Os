FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Make python-services importable
COPY ../python-services /app/python-services
ENV PYTHONPATH=/app:/app/python-services
EXPOSE 8001
CMD ["uvicorn", "web_app_api:app", "--host", "0.0.0.0", "--port", "8001"]
