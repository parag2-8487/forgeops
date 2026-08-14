# SPDX-License-Identifier: Apache-2.0
from .templates import TemplateLoader, TemplateManifest

DEFAULT_TEMPLATES = {
    "nodejs-express": {
        "manifest": TemplateManifest(
            name="nodejs-express", language="nodejs", framework="express", required_variables=["PORT"]
        ),
        "content": (
            "FROM node:20-alpine\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            "RUN npm ci --only=production\n"
            "COPY . .\n"
            "EXPOSE {{PORT}}\n"
            'CMD ["node", "server.js"]'
        ),
    },
    "python-fastapi": {
        "manifest": TemplateManifest(
            name="python-fastapi", language="python", framework="fastapi", required_variables=["PORT"]
        ),
        "content": (
            "FROM python:3.11-slim\n"
            "WORKDIR /app\n"
            "COPY requirements.txt .\n"
            "RUN pip install --no-cache-dir -r requirements.txt\n"
            "COPY . .\n"
            "EXPOSE {{PORT}}\n"
            'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{{PORT}}"]'
        ),
    },
    "go-standard": {
        "manifest": TemplateManifest(name="go-standard", language="go", required_variables=["PORT"]),
        "content": (
            "FROM golang:1.22-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN go build -o server .\n"
            "FROM alpine:latest\n"
            "COPY --from=builder /app/server /server\n"
            "EXPOSE {{PORT}}\n"
            'CMD ["/server"]'
        ),
    },
    "rust-actix": {
        "manifest": TemplateManifest(
            name="rust-actix", language="rust", framework="actix-web", required_variables=["PORT"]
        ),
        "content": (
            "FROM rust:1.75 AS builder\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN cargo build --release\n"
            "FROM debian:bookworm-slim\n"
            "COPY --from=builder /app/target/release/app /usr/local/bin/app\n"
            "EXPOSE {{PORT}}\n"
            'CMD ["app"]'
        ),
    },
    "java-springboot": {
        "manifest": TemplateManifest(
            name="java-springboot", language="java", framework="springboot", required_variables=["PORT"]
        ),
        "content": (
            "FROM eclipse-temurin:21-jre-alpine\n"
            "WORKDIR /app\n"
            "COPY target/*.jar app.jar\n"
            "EXPOSE {{PORT}}\n"
            'ENTRYPOINT ["java", "-jar", "app.jar"]'
        ),
    },
    "ruby-rails": {
        "manifest": TemplateManifest(
            name="ruby-rails", language="ruby", framework="rails", required_variables=["PORT"]
        ),
        "content": (
            "FROM ruby:3.3-slim\n"
            "WORKDIR /app\n"
            "COPY Gemfile Gemfile.lock ./\n"
            "RUN bundle install\n"
            "COPY . .\n"
            "EXPOSE {{PORT}}\n"
            'CMD ["rails", "server", "-b", "0.0.0.0", "-p", "{{PORT}}"]'
        ),
    },
    "php-laravel": {
        "manifest": TemplateManifest(
            name="php-laravel", language="php", framework="laravel", required_variables=["PORT"]
        ),
        "content": (
            "FROM php:8.3-fpm-alpine\n"
            "WORKDIR /var/www\n"
            "COPY . .\n"
            "EXPOSE {{PORT}}\n"
            'CMD ["php", "artisan", "serve", "--host=0.0.0.0", "--port={{PORT}}"]'
        ),
    },
    "dotnet-aspnet": {
        "manifest": TemplateManifest(
            name="dotnet-aspnet", language="dotnet", framework="aspnet", required_variables=["PORT"]
        ),
        "content": (
            "FROM mcr.microsoft.com/dotnet/aspnet:8.0\n"
            "WORKDIR /app\n"
            "COPY bin/Release/net8.0/publish/ .\n"
            "EXPOSE {{PORT}}\n"
            'ENTRYPOINT ["dotnet", "app.dll"]'
        ),
    },
}


def get_default_template_loader() -> TemplateLoader:
    loader = TemplateLoader()
    for _name, item in DEFAULT_TEMPLATES.items():
        loader.register_template(item["manifest"], item["content"])
    return loader
