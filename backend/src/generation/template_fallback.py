# SPDX-License-Identifier: FSL-1.1-ALv2
"""Terminal cascade slot static template fallback (Leaf 13.9)."""

from __future__ import annotations


class TemplateFallback:
    """Provides deterministic static template fallbacks when all AI cascade model slots fail."""

    @staticmethod
    def get_dockerfile_template(python_version: str = "3.11-slim") -> str:
        return f"""# Generated via Terminal Cascade Template Fallback
FROM python:{python_version}
WORKDIR /app
COPY . /app
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
EXPOSE 8000
CMD ["python", "main.py"]
"""

    @staticmethod
    def get_k8s_manifest_template(app_name: str = "app") -> str:
        return f"""# Generated via Terminal Cascade Template Fallback
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {app_name}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {app_name}
  template:
    metadata:
      labels:
        app: {app_name}
    spec:
      containers:
      - name: {app_name}
        image: {app_name}:latest
        ports:
        - containerPort: 8000
"""
