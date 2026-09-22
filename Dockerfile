# 天枢 AI Service — 生产镜像
# 构建：docker build -t tianshu-ai-service:latest .
# 运行：docker compose up -d
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先只拷贝依赖清单并安装，充分利用 Docker 层缓存（代码改动不触发重新装依赖）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝项目代码（.dockerignore 已排除 .env/data/logs/test/docs）
COPY agents/ agents/
COPY api/ api/
COPY core/ core/
COPY skills/ skills/
COPY tools/ tools/
COPY playground/ playground/

# 运行时数据目录（生产环境由 docker-compose 卷挂载覆盖）
RUN mkdir -p data logs

EXPOSE 8300

# uvicorn 直接作为 1 号进程，正确接收 SIGTERM 优雅停机
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8300"]
