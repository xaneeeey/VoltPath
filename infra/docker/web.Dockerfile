# syntax=docker/dockerfile:1.7
FROM node:22-alpine AS build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY infra/docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
RUN chown -R nginx /var/cache/nginx /usr/share/nginx/html && sed -i 's/^user .*//' /etc/nginx/nginx.conf \
    && touch /var/run/nginx.pid && chown nginx /var/run/nginx.pid
USER nginx
EXPOSE 8080
