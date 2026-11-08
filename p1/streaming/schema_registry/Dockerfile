# Confluent Schema Registry + MSK IAM 인증 플러그인 (arm64/amd64 멀티 아키텍처 베이스)
# MSK 는 IAM 인증(9098)만 열어 두었으므로, 저장소 토픽(_schemas)에 붙으려면 aws-msk-iam-auth 가 클래스패스에 있어야 한다.
#   docker buildx build --platform linux/arm64 -t $ECR/danbi-dev/schema-registry:7.9.2-iam --push p1/streaming/schema_registry
FROM confluentinc/cp-schema-registry:7.9.2

ARG IAM_AUTH_VERSION=2.3.2
USER root
RUN curl -fsSL -o /usr/share/java/schema-registry/aws-msk-iam-auth-${IAM_AUTH_VERSION}-all.jar \
      https://github.com/aws/aws-msk-iam-auth/releases/download/v${IAM_AUTH_VERSION}/aws-msk-iam-auth-${IAM_AUTH_VERSION}-all.jar
USER appuser
