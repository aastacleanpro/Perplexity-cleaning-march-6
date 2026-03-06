#!/bin/bash

# Enhanced script to generate secure quantum repository

# Error handling
set -e
set -o pipefail

# Safety checks
if [[ -z "$1" ]]; then
    echo "Usage: $0 <repo-name>"
    exit 1
fi

REPO_NAME="$1"

# Logging function
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
}

log "Starting repo generation..."

# Create the repository structure
mkdir "$REPO_NAME" || { log "Failed to create directory; exiting."; exit 1; }
cd "$REPO_NAME" || { log "Failed to change directory; exiting."; exit 1; }

# Backend structure
mkdir -p backend/{src,config,logs} && touch backend/logs/log.txt
log "Backend structure created."

# Initialize logging
echo "Log file for backend operations" > backend/logs/log.txt

# Docker Compose with health checks
cat <<EOF > docker-compose.yml
version: '3.7'
services:
  app:
    image: myapp:latest
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - '3000:3000'
    healthcheck:
      test: curl --fail http://localhost:3000/ || exit 1
      interval: 30s
      timeout: 10s
      retries: 5
EOF
log "Docker Compose file created with health checks."

# CI/CD workflow configuration
cat <<EOF > .github/workflows/ci.yml
name: CI
on:
  push:
    branches:
      - develop
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v2
      - name: Set up Node.js
        uses: actions/setup-node@v2
        with:
          node-version: '14'
      - name: Install dependencies
        run: npm install
      - name: Run tests
        run: npm test
EOF
log "CI/CD workflow created."

# Frontend setup with TypeScript and testing
mkdir -p frontend
cd frontend
npm init -y
npm install typescript jest ts-jest @types/jest --save-dev
npx tsc --init
log "Frontend setup with TypeScript completed."

# Makefile with development commands
cat <<EOF > Makefile
.PHONY: install test

install:
	npm install

test:
	npm test
EOF
log "Makefile created with development commands."

log "Repository generation completed successfully!"