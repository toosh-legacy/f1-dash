# syntax=docker/dockerfile:1
FROM node:22-alpine

WORKDIR /app

# The dashboard has no build step and no dependencies: the server is stdlib-only
# and the page is static HTML/JS.
COPY frontend/server.js ./frontend/server.js
COPY backend/app/static ./backend/app/static
COPY package.json .

ENV PORT=3000 \
    API_TARGET=http://api:8000 \
    STATIC_ROOT=/app/backend/app/static

USER node
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD node -e "fetch('http://localhost:3000/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "frontend/server.js"]
