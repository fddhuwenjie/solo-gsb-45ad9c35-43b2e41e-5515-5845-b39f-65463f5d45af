FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOCS_DB_PATH=/app/data/docs.db

WORKDIR /app

# 安装依赖（构建镜像层时安装，容器启动即可用）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh ./
RUN chmod +x ./entrypoint.sh

# 数据库持久化目录（Compose 会把命名卷挂到这里，重启后仍可读取）
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

# 容器启动：初始化数据库 -> 启动服务
ENTRYPOINT ["./entrypoint.sh"]
