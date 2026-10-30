FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . ./
ARG VITE_BASE_PATH=/
ENV VITE_BASE_PATH=${VITE_BASE_PATH}
RUN npm run build

FROM node:22-alpine AS runtime
ENV NODE_ENV=production \
    PORT=8080 \
    WEB_API_TARGET=http://web:8000
WORKDIR /app
COPY --from=build --chown=node:node /app/dist ./dist
COPY --chown=node:node server.mjs ./server.mjs
USER node
EXPOSE 8080
CMD ["node", "server.mjs"]
