# Working with coding agents

1. **Discuss until an explicit go.** No code before the scope is agreed (use a grilling pass when the need is vague).
2. **PRD → ~10-15 vertical issues → one issue at a time**, each testable on its own. The agent prepares, codes, tests and runs the harness; a human tests and accepts before the next issue.
3. **Unattended (AFK) runs only for small isolated issues** (a message, a one-screen bug), never for auth, billing boundaries, migrations or a public API.
4. **End of feature:** list the functional edge cases (not only the technical ones), then update the feature page.
5. **Architecture review after each large feature and weekly:** look for shallow modules to deepen behind a narrow interface. It proposes; a human picks; one refactor = one issue.
6. **Parallel lanes:** one issue = one git worktree + one port (8000 stays with the main checkout; lanes take the first free port from 8001) + one database (`DB_SCHEMA=""` SQLite per lane). Remove the worktree when the branch is merged.
7. **Light mode** for a one-line change: harness + the tests of the touched module, no full suite.
