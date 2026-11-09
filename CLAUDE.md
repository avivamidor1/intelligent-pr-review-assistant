# CLAUDE.md

@AGENTS.md

## Project Instructions

You are working on the Intelligent PR Review Assistant.

Before making changes:

1. Read `docs/PLAN.md` to understand the intended architecture and implementation decisions.
2. Inspect the relevant source files and existing project structure.
3. Check the current Git status before modifying files.
4. Understand how the requested change fits into the existing architecture.

## How to Work

- Work incrementally. Prefer small, focused changes over large rewrites.
- Do not implement functionality that has not been requested or defined in the plan.
- Do not assume that a planned feature already exists. Verify the current implementation first.
- Reuse existing patterns and components whenever possible.
- Avoid unnecessary refactoring while working on a specific task.
- If a requirement is ambiguous or conflicts with the architecture, explain the issue and ask before making assumptions.
- If an architectural change is genuinely necessary, explain why and propose the smallest reasonable change before proceeding.

## Implementation Workflow

For non-trivial tasks:

1. Explain your understanding of the task.
2. Identify the relevant files and components.
3. Outline a short implementation plan.
4. Implement the change in focused steps.
5. Add or update relevant tests.
6. Run the appropriate checks and inspect the final changes.

Do not stop at planning when the task clearly requests implementation.

## Documentation

- Keep documentation consistent with the actual implementation.
- Update documentation when an approved change affects documented behavior or architecture.
- Do not modify `docs/PLAN.md` simply to justify an implementation that deviates from the existing plan.
- Keep `AGENTS.md` focused on engineering rules and `docs/PLAN.md` focused on technical decisions and implementation details.

## Communication

At the end of each task, provide:

- **What changed:** A concise summary of the implementation.
- **Files changed:** The relevant files.
- **Verification:** Tests and checks actually executed, including their results.
- **Limitations:** Any known issues, incomplete work, or unverified behavior.

Be direct and transparent. Never claim something works unless it has been verified.