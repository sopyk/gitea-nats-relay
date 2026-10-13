FROM python:3.12-alpine
WORKDIR /app
RUN pip install --no-cache-dir "nats-py>=2.16,<3"
# 只烤**产品代码**。测试文件里有测试用的密钥占位符和内网主机名，
# 不该出现在交付给别人的镜像里；测试在源码树里跑，不需要进镜像。
# logs.py 必须一起进 —— server.py 和 core.py 都 import 它，漏了直接 ImportError。
COPY app.py config.py core.py logs.py providers.py server.py /app/
# 刻意不设 MEMBERS_FILE 等路径类环境变量：它们优先级高于配置文件，
# 在镜像里烤死默认值会让 config/webhook2nats.toml 里的对应项永远不生效。
# /version 端点报的两个值，都由 CI 传 --build-arg 注入：
#   VERSION → 发布版本号（仓库根的 VERSION 文件）
#   GIT_SHA → 精确到 commit
#
# 刻意不读 .git：交付镜像里本来就没有它，多阶段构建里读了只是无谓耦合。
# 本地 docker build 不传就是空，代码里回落成 "unknown"。
#
# 版本号**只**从构建期注入，不在镜像里烤死 —— 否则每次发版都要改 Dockerfile，
# 而那正是「版本号出现在多处、有的改了有的没改」的经典成因。
ARG VERSION=""
ENV WEBHOOK2NATS_VERSION=$VERSION

ARG GIT_SHA=""
ENV WEBHOOK2NATS_COMMIT=$GIT_SHA

ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=3)" || exit 1
CMD ["python", "-u", "/app/app.py"]