# Build: VITE_* settings are baked in at build time. Pass a file with the target environment's
# values (ENV_FILE), or set them as build args in your pipeline.
FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG ENV_FILE=.env.example
RUN cp "$ENV_FILE" .env.production.local && npm run build

# Serve the static files; every unknown path falls back to index.html (client-side routes).
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
