FROM nginx:alpine
# Template processado por envsubst no start (substitui ${API_UPSTREAM}).
COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY index.html /usr/share/nginx/html/index.html
# Default para dev/compose; sobrescreva no Coolify com o hostname interno do backend.
ENV API_UPSTREAM=http://backend:8000
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1/health || exit 1
CMD ["nginx", "-g", "daemon off;"]
