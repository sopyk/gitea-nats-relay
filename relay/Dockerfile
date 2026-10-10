FROM python:3.12-alpine
WORKDIR /app
RUN pip install --no-cache-dir "nats-py>=2.16,<3"
# 只烤代码，不烤身份映射：members.json 由宿主 ./config 只读挂载到 /config
COPY *.py /app/
ENV MEMBERS_FILE=/config/members.json
ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=3)" || exit 1
CMD ["python", "-u", "/app/app.py"]