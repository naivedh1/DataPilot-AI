# syntax=docker/dockerfile:1
#
# Builds the SPA and serves it from nginx. The runtime image contains only
# static assets and nginx - no Node, no npm, no source.

# --------------------------------------------------------------------------- #
# Builder                                                                      #
# --------------------------------------------------------------------------- #
FROM node:22-alpine AS builder

WORKDIR /build

# Lockfile-driven install, cached independently of source changes.
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY . .

# Requests go to a relative /api path, which nginx proxies. Baking an absolute
# backend URL into the bundle would tie the image to one deployment.
ENV VITE_API_BASE_URL=""
RUN npm run build

# --------------------------------------------------------------------------- #
# Runtime                                                                      #
# --------------------------------------------------------------------------- #
FROM nginx:1.27-alpine AS runtime

COPY --from=builder /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=4s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
