# 대사·정산 파일 적재 이미지 (ECS RunTask)
#   docker buildx build --platform linux/arm64 -f p1/recon/Dockerfile -t $ECR/danbi-dev/recon:<sha> --push .
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app:/app/libs
WORKDIR /app
RUN pip install --no-cache-dir "pyiceberg[glue,pyarrow]==0.10.*" boto3==1.40.* pymysql==1.1.* "psycopg[binary]==3.2.*"
COPY libs/ libs/
COPY p1/__init__.py p1/__init__.py
COPY p1/recon/ p1/recon/
COPY p1/generator/__init__.py p1/generator/cli.py p1/generator/
RUN useradd --uid 10001 app
USER 10001
ENTRYPOINT ["python", "-m", "p1.recon.cli"]
