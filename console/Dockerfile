# Knowledge Console.
#
# nginx serving static files, and proxying /v1 and /readyz through to the api
# service. The proxy is the point: the console and the API share an origin, so
# there is no CORS configuration to get wrong and no credential that has to be
# readable by browser JavaScript to be usable.
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY public/ /usr/share/nginx/html/

EXPOSE 3000

# 127.0.0.1 rather than `localhost`: busybox resolves localhost to ::1 first, so
# a name-based check depends on IPv6 being configured and fails for a reason
# that has nothing to do with whether the console is serving.
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD wget -qO- http://127.0.0.1:3000/ >/dev/null 2>&1 || exit 1
