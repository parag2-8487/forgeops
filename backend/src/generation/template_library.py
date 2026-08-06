# SPDX-License-Identifier: Apache-2.0
from src.generation.templates import TemplateLoader, TemplateManifest

DEFAULT_TEMPLATES = {
    "nodejs-express": {
        "manifest": TemplateManifest(
            name="nodejs-express",
            language="nodejs",
            framework="express",
            required_variables=["PORT"]
        ),
        "content": "FROM node:20-alpine\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci --only=production\nCOPY . .\nEXPOSE {{PORT}}\nCMD [\"node\", \"server.js\"]"
    },
    "python-fastapi": {
        "manifest": TemplateManifest(
            name="python-fastapi",
            language="python",
            framework="fastapi",
            required_variables=["PORT"]
        ),
        "content": "FROM python:3.11-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY . .\nEXPOSE {{PORT}}\nCMD [\"uvicorn\", \"main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"{{PORT}}\"]"
    },
    "go-standard": {
        "manifest": TemplateManifest(
            name="go-standard",
            language="go",
            required_variables=["PORT"]
        ),
        "content": "FROM golang:1.22-alpine AS builder\nWORKDIR /app\nCOPY . .\nRUN go build -o server .\nFROM alpine:latest\nCOPY --from=builder /app/server /server\nEXPOSE {{PORT}}\nCMD [\"/server\"]"
    },
    "rust-actix": {
        "manifest": TemplateManifest(
            name="rust-actix",
            language="rust",
            framework="actix-web",
            required_variables=["PORT"]
        ),
        "content": "FROM rust:1.75 AS builder\nWORKDIR /app\nCOPY . .\nRUN cargo build --release\nFROM debian:bookworm-slim\nCOPY --from=builder /app/target/release/app /usr/local/bin/app\nEXPOSE {{PORT}}\nCMD [\"app\"]"
    },
    "java-springboot": {
        "manifest": TemplateManifest(
            name="java-springboot",
            language="java",
            framework="springboot",
            required_variables=["PORT"]
        ),
        "content": "FROM eclipse-temurin:21-jre-alpine\nWORKDIR /app\nCOPY target/*.jar app.jar\nEXPOSE {{PORT}}\nENTRYPOINT [\"java\", \"-jar\", \"app.jar\"]"
    },
    "ruby-rails": {
        "manifest": TemplateManifest(
            name="ruby-rails",
            language="ruby",
            framework="rails",
            required_variables=["PORT"]
        ),
        "content": "FROM ruby:3.3-slim\nWORKDIR /app\nCOPY Gemfile Gemfile.lock ./\nRUN bundle install\nCOPY . .\nEXPOSE {{PORT}}\nCMD [\"rails\", \"server\", \"-b\", \"0.0.0.0\", \"-p\", \"{{PORT}}\"]"
    },
    "php-laravel": {
        "manifest": TemplateManifest(
            name="php-laravel",
            language="php",
            framework="laravel",
            required_variables=["PORT"]
        ),
        "content": "FROM php:8.3-fpm-alpine\nWORKDIR /var/www\nCOPY . .\nEXPOSE {{PORT}}\nCMD [\"php\", \"artisan\", \"serve\", \"--host=0.0.0.0\", \"--port={{PORT}}\"]"
    },
    "dotnet-aspnet": {
        "manifest": TemplateManifest(
            name="dotnet-aspnet",
            language="dotnet",
            framework="aspnet",
            required_variables=["PORT"]
        ),
        "content": "FROM mcr.microsoft.com/dotnet/aspnet:8.0\nWORKDIR /app\nCOPY bin/Release/net8.0/publish/ .\nEXPOSE {{PORT}}\nENTRYPOINT [\"dotnet\", \"app.dll\"]"
    }
}


def get_default_template_loader() -> TemplateLoader:
    loader = TemplateLoader()
    for name, item in DEFAULT_TEMPLATES.items():
        loader.register_template(item["manifest"], item["content"])
    return loader
