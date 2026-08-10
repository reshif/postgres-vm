# Knowledge Console: statically exported Next.js application served by nginx.
# The console talks to the API only through nginx's same-origin proxy, never to
# PostgreSQL. The build generates CSP hashes for Next's tiny RSC bootstrap
# scripts, so stored content remains unable to execute arbitrary JavaScript.
FROM node:22.13-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . ./
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/out/ /usr/share/nginx/html/

EXPOSE 3000

# 127.0.0.1 rather than `localhost`: busybox resolves localhost to ::1 first, so
# a name-based check depends on IPv6 being configured and fails for a reason
# that has nothing to do with whether the console is serving.
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD wget -qO- http://127.0.0.1:3000/ >/dev/null 2>&1 || exit 1
