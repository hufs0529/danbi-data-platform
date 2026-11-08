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

.PHONY: help init plan apply destroy output pause resume lint test tf-fmt tf-validate validate-all

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
