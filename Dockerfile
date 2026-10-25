# GPT-monitor 面板 + 监控循环
# 基础镜像走国内加速站（Docker Hub 直连被墙）；如失效可换 docker.1ms.run/library/python:3.12-slim
FROM docker.m.daocloud.io/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# tzdata: 让容器内日志时间戳走 Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# 国内构建慢可用: --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt

COPY monitor.py app.py ./
COPY static ./static

EXPOSE 8787
CMD ["python", "app.py"]
