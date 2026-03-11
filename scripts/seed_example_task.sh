#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VAULT_DIR="$PROJECT_DIR/ExampleVault"

echo "=== Seeding example context and task ==="

# Create ME context
mkdir -p "$VAULT_DIR/ME"
cat > "$VAULT_DIR/ME/_context.md" << 'EOF'
---
type: context
---

Personal workspace. This is the top-level context for all my projects and tasks.
EOF

# Create Test Project context
mkdir -p "$VAULT_DIR/ME/Test Project"
cat > "$VAULT_DIR/ME/Test Project/_context.md" << 'EOF'
---
type: context
tech_stack:
  - python
  - docker
---

A test project for verifying DavyJones works end-to-end.
EOF

# Create a sample task
cat > "$VAULT_DIR/ME/Test Project/hello_task.md" << 'EOF'
---
type: task
status: pending
prompt: "Write a short greeting message and save it to ME/Test Project/greeting_result.md. The result file should have type: note in its YAML frontmatter and contain a friendly hello message."
---

# Hello Task

This is a test task to verify the DavyJones system works end-to-end.
The agent should create a new file with the greeting.
EOF

# Commit the seed data
cd "$VAULT_DIR"
git add -A
git commit -m "Seed: add example context hierarchy and test task"

echo ""
echo "=== Seed complete ==="
echo "Created:"
echo "  - ME/_context.md"
echo "  - ME/Test Project/_context.md"
echo "  - ME/Test Project/hello_task.md (status: pending)"
echo ""
echo "Start the system with: docker compose up"
echo "Watch the agent process hello_task.md and create greeting_result.md"
