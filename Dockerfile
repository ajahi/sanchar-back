FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# pip is only needed to install; removing it in the same layer keeps it out of the image
RUN pip install --no-cache-dir -r requirements.txt \
 && pip uninstall -y pip setuptools wheel
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000" , "--workers", "4"]
