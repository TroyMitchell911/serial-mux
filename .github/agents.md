# `.github/` Development Guide

The workflow under `.github/workflows/ci.yml` is the CI contract.

Current CI:

- runs on pushes and pull requests targeting `main`;
- tests Python 3.10 and 3.12 on Ubuntu;
- installs `socat` and `openssh-server`;
- creates localhost SSH keys/authorized keys and verifies key-based SSH;
- installs `pip install -e ".[test]"`;
- runs `python3 -m pytest tests/ -v --timeout=60`.

Keep CI aligned with the real test prerequisites. If a new external tool, Python version, environment variable, or opt-in integration test is introduced, update the workflow and the test documentation together. Do not make ordinary tests depend on physical serial hardware, a developer's SSH configuration, or external network services.

Use the repository's existing GitHub Actions major versions unless there is a concrete compatibility reason to change them. Validate workflow YAML and run the equivalent local pytest command before changing the workflow. Keep secrets and device credentials out of workflow logs.
