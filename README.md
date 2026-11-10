# Adrian Code Agent

Local-first coding agent for terminal workflows, inspired by the operational model of Claude Code but intentionally simplified and tailored for a local, controllable developer environment.

<p align="center">
  <img src="https://img.shields.io/badge/Status-Local%20Agent-0ea5e9" alt="Status" />
  <img src="https://img.shields.io/badge/Mode-Tool%20Calling-22c55e" alt="Mode" />
  <img src="https://img.shields.io/badge/Memory-Session%20%2B%20Long%20Term-f59e0b" alt="Memory" />
  <img src="https://img.shields.io/badge/Safety-Approval%20Guardrails-ef4444" alt="Safety" />
</p>

## Why this project

Adrian Code Agent is a private, local-first coding assistant designed for the same core operating model that modern agentic IDEs and terminal tools rely on: persistent context, structured tool use, task orchestration, memory retrieval, and safety-aware execution.

It is not a generic chatbot bolted onto a terminal. It is a lightweight, maintainable agent runtime built for real coding work: inspect files, patch code, run commands, manage background jobs, recall prior context, and keep risky actions under explicit control. The spirit is intentionally aligned with tools like Codex and Claude Code, but the implementation stays smaller, more transparent, and easier to extend locally.

## What it does

Adrian Code Agent is built around a practical operating model for local software work:

- REPL-driven agent loop for iterative coding workflows
- File-aware editing and context grounding for targeted code changes
- Background task orchestration for tests, builds, and long-running jobs
- Persistent session memory with retrieval and summarization
- Multi-agent delegation for specialized investigation and execution
- MCP-based extensibility for external tools and integrations
- Safety-first policy engine with approval gating and plan-mode controls

## Core capabilities

### 1. Terminal-native orchestration

The agent runs as a persistent terminal workflow with operational controls designed for real developer use:

- /new — start a fresh session
- /resume — restore a previous session
- /rewind — jump back to an earlier checkpoint
- /compact — proactively compress context and keep memory efficient
- /status — inspect session health and token usage
- /jobs — monitor background execution
- /agents — inspect available agent roles
- /mcp — inspect tool integrations
- /memory — review memory state
- /dream — trigger memory consolidation
- /api-detail — inspect recent request telemetry
- /help — surface the command surface
- /exit — leave the session cleanly

This command layer is intentionally compact but operationally useful: it gives the user direct control over agent lifecycle, session state, and execution flow.

### 2. Context-aware code operations

The agent is built for direct software workflow, not just text generation:

- targeted file reads with range-aware access
- partial in-place edits without destructive rewrites
- full-file writes when needed
- @-mention injection for grounding the model in repo context
- file history and checkpoint tracking for safer iteration

This creates a grounded editing loop rather than a blind assistant that writes without understanding the surrounding code.

### 3. Background execution and task management

The runtime supports long-running development work through a task/job system:

- foreground command execution with safety boundaries
- background scheduling and monitoring
- job logs and execution tracking
- cancellation of stuck or unwanted processes
- structured task tracking across sessions

This is essential for running tests, dev servers, background checks, and monitoring workflows without interrupting the main agent loop.

### 4. Memory, retrieval, and context compaction

The project includes a lightweight memory stack that behaves more like an operational coding memory than a passive chat log:

- persistent learning across sessions
- retrieval of relevant context before task execution
- RAG-style memory grounding for repeated project work
- regular consolidation via background memory processing
- automatic prompt compaction to keep the model within a healthy context window

The result is an agent that does not forget its prior work and does not drift into context bloat.

### 5. Multi-agent coordination

The system supports a multi-agent pattern for specialized work:

- explore — read-only code investigation
- plan — architecture and solution planning
- general — end-to-end task execution

This turns the system into a small collaborative agent runtime instead of a single oversized monolith, with each role optimized for a different phase of engineering work.

### 6. Tool policy and intelligent safety

Safety is a first-class feature, not a cosmetic layer:

- five permission modes: default, acceptEdits, plan, auto, bypass
- tool-level policy decisions before execution
- auto analysis of whether a tool call should be allowed or blocked
- allowlist / denylist style evaluation for risky operations
- user approval flow for high-impact actions
- session-scoped trust decisions to reduce repeated prompting

This is the layer that keeps the agent capable while making it controllable in real projects.

### 7. MCP extensibility and telemetry

The project is designed to expand beyond the local core:

- external MCP tool integration
- request-level telemetry and inspection
- runtime observability for model/tool execution
- a modular architecture that stays easy to extend without turning into a fragile monolith

In short, this project is a maintainable local agent runtime that captures the important workflow patterns of modern coding assistants without adopting the full cost and complexity of a large commercial stack.

## Product philosophy

This is a compact local implementation of the operational model used by more advanced coding agents: persistent sessions, structured tool access, memory-grounded context, planning, and safety-aware execution.

The emphasis is on:

- transparency: the user can inspect what the agent is doing
- control: behavior is bounded by explicit permissions and tool policy
- locality: the workflow stays in the developer environment by default
- precision: tasks can be reduced to investigation, planning, editing, validation, and follow-up
- continuity: memory, task tracking, and checkpoints keep the system useful over time

The goal is not to clone every feature of a commercial product verbatim. It is to preserve the essential product experience of a serious local coding assistant while staying lightweight, readable, and maintainable.

## Quick start

### Install

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

If you do not yet have a requirements file, install the core runtime manually:

```bash
pip install pydantic-ai rich prompt_toolkit questionary
```

### Configure model access

Set your API key in the environment:

```bash
export API_KEY="your_api_key_here"
```

Or in PowerShell:

```powershell
$env:API_KEY="your_api_key_here"
```

### Run

```bash
python main.py
```

Once running, you can begin with tasks like:

- inspect a repo and explain the structure
- fix a bug in a targeted file
- create a plan before making large edits
- run tests or a local dev server in the background
- delegate a code investigation to a focused sub-agent

## Project structure

- main.py — session entry point and REPL loop
- agent/ — model, tool registration, hooks, and sub-agent logic
- ui/ — terminal rendering and command UI
- memory/ — recall, extraction, and memory indexing
- background_jobs.py — background task lifecycle and notifications
- permissions.py — safety and approval logic
- plan_mode.py — plan-first execution mode
- tasks_store.py — structured task state persistence
- mcp_servers.py — MCP service discovery and integration
- skills.py — custom skill discovery and loading
- session.py — session file management and history recovery

## Local data paths

The project stores local state under the user home directory, commonly under:

- ~/.my-code-agent/
- ~/.my-code-agent/tasks/
- ~/.my-code-agent/jobs/
- ~/.my-code-agent/skills/
- ~/.my-code-agent/plans/
- ~/.my-code-agent/mcp.json

This keeps the agent stateful without forcing the repository itself to hold operational metadata.

## License

This project is licensed under the MIT License. See the LICENSE file for details.
