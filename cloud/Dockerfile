FROM python:3.11-slim

WORKDIR /app

# Build context is the project root
COPY cloud/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cloud/ ./cloud/

# Include vault K8s template so provisioner can find it at /app/k8s/vault-template.yaml
COPY k8s/base/vault-template.yaml ./k8s/vault-template.yaml

EXPOSE 8000

CMD ["uvicorn", "cloud.main:app", "--host", "0.0.0.0", "--port", "8000"]
