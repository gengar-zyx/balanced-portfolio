FROM docker.m.daocloud.io/library/python:3.13-slim AS backend

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bp_api bp_api
COPY bp_ingest bp_ingest
COPY ddl ddl

FROM docker.m.daocloud.io/library/node:22-bookworm-slim AS web-build
WORKDIR /app/web
ARG BP_API_BASE=http://api:8000
ENV BP_API_BASE=${BP_API_BASE}
COPY web/package.json web/package-lock.json ./
RUN npm ci --legacy-peer-deps
COPY web/ ./
RUN npm run build

FROM docker.m.daocloud.io/library/node:22-bookworm-slim AS web
ENV NODE_ENV=production
WORKDIR /app/web
COPY --from=web-build /app/web ./
EXPOSE 3000
CMD ["npm", "run", "start"]
