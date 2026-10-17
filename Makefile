.PHONY: test ui-test lint validate check quickstart quickstart-down gates clean

RENDERED_WORKSPACE_DIR := .rendered-workspace/validate
SHELL_SCRIPTS := scripts/*.sh scripts/phases/*.sh scripts/ops/*.sh

# Cloud provisioning goes through scripts/install.sh; see docs/runbooks/install.md.

test:
	GATEWAY_TOKEN=test-token RUNTIME_FAKE_PROVIDER=true TS_HARNESS_SKIP_MCP_DISCOVERY=true \
		python3 -m pytest tests -q

ui-test:
	cd admin-ui && npm test

lint:
	ruff check runtime tests scripts
	shellcheck --severity=warning $(SHELL_SCRIPTS)
	terraform fmt -check -recursive infra
	gitleaks dir . --no-banner --redact

validate:
	rm -rf $(RENDERED_WORKSPACE_DIR)
	python3 scripts/render-workspace.py --config config/organization.example.yaml --src workspace --dst $(RENDERED_WORKSPACE_DIR) --strict
	bash scripts/validate-placeholders.sh $(RENDERED_WORKSPACE_DIR)
	bash scripts/validate-placeholders.sh skills
	python3 scripts/generate-agent-docs.py --check

check: lint validate test

quickstart:
	bash scripts/quickstart.sh up

quickstart-down:
	bash scripts/quickstart.sh down

gates:
	bash scripts/quickstart.sh gates

clean:
	rm -rf .rendered-workspace build *.egg-info
