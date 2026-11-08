# 생성기 + 소스 DB 마이그레이션 이미지 (arm64, Fargate Graviton)
# 빌드: 저장소 루트에서
#   docker buildx build --platform linux/arm64 -f p1/generator/Dockerfile -t $ECR/danbi-dev/generator:$(git rev-parse --short HEAD) --push .
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app:/app/libs
WORKDIR /app

RUN pip install --no-cache-dir \
      boto3==1.40.* pymysql==1.1.* "psycopg[binary]==3.2.*" \
      confluent-kafka==2.11.* fastavro==1.12.* httpx==0.28.* attrs cachetools authlib \
      aws-msk-iam-sasl-signer-python==1.0.*

COPY libs/ libs/
COPY contracts/avro/ contracts/avro/
COPY p1/__init__.py p1/__init__.py
COPY p1/sources/ p1/sources/
COPY p1/generator/ p1/generator/

RUN useradd --uid 10001 --no-create-home app
USER 10001

ENTRYPOINT ["python", "-m"]
CMD ["p1.generator.cli", "run"]
