FROM python:3.11-slim

# GCC 工具链 + 硬链接所需的 coreutils（slim 已含，显式声明更稳妥）
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 快照 / 目标文件缓存 / 构建产物目录（可挂载卷持久化缓存）
RUN mkdir -p /workspace/data
ENV BUILD_WORKSPACE=/workspace/data \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# 单进程多线程即可承载并行构建（构建在线程池中运行）
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
