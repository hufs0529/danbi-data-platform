# dbt 실행기 (ECS RunTask: MWAA 가 EcsRunTaskOperator 로 부른다)
#   docker buildx build --platform linux/arm64 -f p1/dbt/docker/Dockerfile -t $ECR/danbi-dev/dbt-runner:$(git rev-parse --short HEAD) --push .
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DBT_PROFILES_DIR=/app/dbt DO_NOT_TRACK=1
RUN pip install --no-cache-dir dbt-core==1.12.* dbt-athena-community==1.11.* openlineage-dbt==1.40.* awscli==1.42.*
WORKDIR /app/dbt
COPY p1/dbt/ /app/dbt/
RUN rm -rf target logs && dbt parse --no-partial-parse   # 이미지 빌드 시점에 구문 오류를 잡는다
RUN useradd --uid 10001 app && chown -R app /app
USER 10001
ENTRYPOINT ["/app/dbt/docker/run.sh"]
