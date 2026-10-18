FROM nginx:1.28-alpine
COPY deploy/gcp/gateway/nginx.conf.template /etc/nginx/workbench.conf.template
ENV PORT=8080
USER 101:101
EXPOSE 8080
ENTRYPOINT []
CMD ["/bin/sh", "-c", "envsubst '$PORT $WORKBENCH_UPSTREAM' < /etc/nginx/workbench.conf.template > /tmp/nginx.conf && exec nginx -c /tmp/nginx.conf -g 'daemon off;'"]
