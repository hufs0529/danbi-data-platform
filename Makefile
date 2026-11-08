SHELL := /bin/bash
ENV   ?= dev
STACK ?=
TF    ?= terraform
ROOT  := $(shell pwd)
STACK_DIR := infra/stacks/$(STACK)

# 비싼 구성이 들어 있는 스택. pause/resume 대상
PAUSABLE := 10-network p1-20-streaming p1-40-processing p1-50-orchestration p2-10-apps p2-20-knowledge p2-30-pipeline p2-40-serving

define var_files
-var-file=$(ROOT)/infra/envs/$(ENV)/common.tfvars $(if $(wildcard infra/envs/$(ENV)/$(1).tfvars),-var-file=$(ROOT)/infra/envs/$(ENV)/$(1).tfvars,)
endef

.PHONY: help init plan apply destroy output pause resume lint test tf-fmt tf-validate validate-all gen-start gen-stop db-migrate topics schemas spark-job

help:
	@echo "make init|plan|apply|destroy STACK=<name> [ENV=dev] [TF=terraform|tofu]"
	@echo "make pause | resume        # 비싼 구성 끄기/켜기"
	@echo "make lint | test | tf-fmt | tf-validate"

guard-stack:
	@test -n "$(STACK)" || (echo "STACK=<infra/stacks 아래 이름> 이 필요하다" && exit 1)
	@test -d "$(STACK_DIR)" || (echo "$(STACK_DIR) 가 없다" && exit 1)

init: guard-stack
	cd $(STACK_DIR) && $(TF) init -reconfigure \
	  -backend-config=$(ROOT)/infra/backend.hcl \
	  -backend-config="key=$(ENV)/$(STACK).tfstate"

plan: guard-stack
	cd $(STACK_DIR) && $(TF) plan $(call var_files,$(STACK)) -out=tfplan

apply: guard-stack
	cd $(STACK_DIR) && $(TF) apply tfplan

destroy: guard-stack
	cd $(STACK_DIR) && $(TF) destroy $(call var_files,$(STACK))

output: guard-stack
	cd $(STACK_DIR) && $(TF) output

# pause: paused/<stack>.tfvars 를 덧씌워 비싼 리소스를 끈다 (MSK·MWAA·OpenSearch 삭제, NAT 제거, Flink 정지, ECS 0대)
pause:
	@for s in $(PAUSABLE); do \
	  if [ -f infra/envs/paused/$$s.tfvars ]; then \
	    echo "== pause $$s"; \
	    (cd infra/stacks/$$s && $(TF) apply -auto-approve $(call var_files,$$s) -var-file=$(ROOT)/infra/envs/paused/$$s.tfvars) || exit 1; \
	  fi; \
	done
	python3 scripts/rds_power.py stop --env $(ENV)

resume:
	python3 scripts/rds_power.py start --env $(ENV)
	@for s in $(PAUSABLE); do \
	  echo "== resume $$s"; \
	  (cd infra/stacks/$$s && $(TF) apply -auto-approve $(call var_files,$$s)) || exit 1; \
	done

lint:
	ruff check .
	ruff format --check .

test:
	pytest -q

tf-fmt:
	$(TF) fmt -check -recursive infra

tf-validate:
	./scripts/tf_validate_all.sh $(TF)

validate-all: lint test tf-fmt tf-validate

# ---------------------------------------------------------------------------
# P1 운영 단축 명령 (AWS CLI). 값은 SSM 파라미터에서 읽는다
# ---------------------------------------------------------------------------
P   := /danbi/$(ENV)
ssm = $(shell aws ssm get-parameter --name $(P)/$(1) --query Parameter.Value --output text)

# 생성기 켜기/끄기. Terraform 은 desired_count=0 이 기본이라 다음 apply 때 다시 꺼진다(켜 둔 채 잊는 비용 방지)
gen-start:
	aws ecs update-service --cluster $(call ssm,platform/ecs_cluster_name) --service $(call ssm,p1/generator/service_name) --desired-count 1 >/dev/null && echo started

gen-stop:
	aws ecs update-service --cluster $(call ssm,platform/ecs_cluster_name) --service $(call ssm,p1/generator/service_name) --desired-count 0 >/dev/null && echo stopped

# 소스 DB 마이그레이션 + 앱 계정 생성(ENGINE=mysql|postgres). VPC 안에서 실행되므로 포트 포워딩이 필요 없다
ENGINE ?= mysql
db-migrate:
	aws ecs run-task --cluster $(call ssm,platform/ecs_cluster_name) \
	  --task-definition $(call ssm,p1/generator/migrate_task_family) --launch-type FARGATE \
	  --network-configuration "awsvpcConfiguration={subnets=[$(call ssm,network/app_subnet_ids)],securityGroups=[$(call ssm,p1/sources/db_clients_sg_id)]}" \
	  --overrides '{"containerOverrides":[{"name":"app","command":["p1.sources.migrate","--engine","$(ENGINE)","--bootstrap-users"]}]}' \
	  --query 'tasks[0].taskArn' --output text

# 토픽 as code / 스키마 레지스트리 (ACTION=plan|apply, SR=config|check|register|list)
ACTION ?= plan
SR ?= check
define run_admin
	aws ecs run-task --cluster $(call ssm,platform/ecs_cluster_name) \
	  --task-definition $(call ssm,p1/streaming/admin_task_family) --launch-type FARGATE \
	  --network-configuration "awsvpcConfiguration={subnets=[$(call ssm,network/app_subnet_ids)],securityGroups=[$(call ssm,p1/sources/msk_clients_sg_id)]}" \
	  --overrides '{"containerOverrides":[{"name":"app","command":$(1)}]}' --query 'tasks[0].taskArn' --output text
endef

topics:
	$(call run_admin,["p1.streaming.topics","$(ACTION)"])

schemas:
	$(call run_admin,["p1.streaming.schema_registry.admin","$(SR)"])

# EMR Serverless 잡 실행: make spark-job JOB=silver_merge ARGS="--tables silver.orders"
JOB ?= silver_merge
ARGS ?=
spark-job:
	aws emr-serverless start-job-run --application-id $(call ssm,p1/processing/emr_app_id) \
	  --execution-role-arn $(call ssm,p1/processing/emr_job_role_arn) --name $(JOB)-manual \
	  --job-driver '{"sparkSubmit":{"entryPoint":"$(call ssm,p1/processing/spark_entry_prefix)/$(JOB).py","entryPointArguments":[$(if $(ARGS),$(shell python3 -c 'import json,sys;print(",".join(json.dumps(a) for a in sys.argv[1:]))' $(ARGS)),)],"sparkSubmitParameters":"$(call ssm,p1/processing/spark_submit_params)"}}' \
	  --configuration-overrides '{"monitoringConfiguration":{"s3MonitoringConfiguration":{"logUri":"$(call ssm,p1/processing/emr_log_uri)"}}}' \
	  --query jobRunId --output text
