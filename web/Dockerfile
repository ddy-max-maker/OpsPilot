# OpsPilot Console 镜像（Vue 3 + Vite 多阶段构建）
#
# 阶段 1：Node 构建静态产物（源码在 console/，依赖锁定 package-lock.json）
# 阶段 2：nginx 托管 dist + 反向代理 /api → api:8000（同源，无 CORS）
#
# D7 说明：D1 的 web/static 原生 JS 控制台已被 D6 的 Vue 控制台取代，
# 本镜像只托管 console 构建产物（唯一 UI 来源）。

FROM node:22-alpine AS build

WORKDIR /app

# 构建期 npm 镜像（默认 npmmirror：与后端 PyPI 清华源同理，主网络对
# registry.npmjs.org 不稳定）。海外网络可传 --build-arg NPM_REGISTRY=https://registry.npmjs.org
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN npm config set registry "$NPM_REGISTRY"

# 先装依赖（利用层缓存：仅 package*.json 变化才重装）
COPY console/package.json console/package-lock.json ./
RUN npm ci --no-audit --no-fund

# 再构建（tsconfig / vite 配置 / 入口与源码）
COPY console/tsconfig.json console/vite.config.ts console/index.html ./
COPY console/src ./src
RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

EXPOSE 80
