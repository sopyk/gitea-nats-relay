FROM python:3.12-alpine
WORKDIR /app
RUN pip install --no-cache-dir "nats-py>=2.16,<3"
# 只烤**产品代码**。测试文件里有测试用的密钥占位符和内网主机名，
# 不该出现在交付给别人的镜像里；测试在源码树里跑，不需要进镜像。
# logs.py 必须一起进 —— server.py 和 core.py 都 import 它，漏了直接 ImportError。
COPY app.py config.py core.py logs.py providers.py server.py /app/
# 刻意不设 MEMBERS_FILE 等路径类环境变量：它们优先级高于配置文件，
# 在镜像里烤死默认值会让 config/webhook2nats.toml 里的对应项永远不生效。
# /version 端点要报的 git SHA。由 CI 传 --build-arg GIT_SHA=${{ github.sha }}。
# 本地 docker build 不传就是空，代码里回落成 "unknown" —— 刻意不去读 .git，
# 交付镜像里本来就没有，而且读了会在多阶段构建里制造无谓的耦合。
ARG GIT_SHA=""
ENV WEBHOOK2NATS_VERSION=$GIT_SHA

ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=3)" || exit 1
CMD ["python", "-u", "/app/app.py"]