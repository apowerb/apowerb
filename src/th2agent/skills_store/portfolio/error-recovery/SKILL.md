---
name: error-recovery
description: "Handle tool errors and failures gracefully. Use when a tool call returns an error, a query fails, a connection times out, or any operation does not succeed. Provides retry strategies and user-friendly error communication. Keywords - error, failure, retry, troubleshoot, fix, problem, issue, failed tool, timeout, connection error, permission denied."
---

# Error Recovery

You are an expert at diagnosing and recovering from tool errors. When any tool call fails, follow this systematic process.

## Step 1: Read the Error Message

Extract the key information from the error:

- **Error type**: What category of error is it? (authentication, connection, validation, permission, data, rate limit)
- **Error details**: What specific message or code was returned?
- **Context**: Which tool failed? What arguments were passed?

Never expose raw error messages, stack traces, or technical dumps to the user. Always translate into plain language.

## Step 2: Classify and Act

### Authentication Errors
**Patterns**: "token expired", "invalid credentials", "unauthorized", "401", "authentication failed"

**Action**: Do NOT retry. The user's connection to the service needs to be refreshed.

**Response to user**: "Your connection to [service name] has expired. Please reconnect it from the Integrations page in Settings, then try again."

### Connection Errors
**Patterns**: "timeout", "connection refused", "unreachable", "ECONNREFUSED", "503", "network error"

**Action**: Retry ONCE after informing the user.

**Response to user**: "The connection to [service name] timed out. Let me try again." Then retry the same tool call once. If it fails again, inform the user that the service may be temporarily unavailable.

### Validation Errors
**Patterns**: "invalid input", "missing required field", "bad request", "400", "validation failed"

**Action**: Fix the input and retry.

- Review the tool arguments you passed.
- Check for missing required parameters.
- Check for wrong data types (string vs number).
- Fix the input and retry once.

### Permission Errors
**Patterns**: "forbidden", "access denied", "403", "insufficient permissions", "not authorized"

**Action**: Do NOT retry. The user does not have access.

**Response to user**: "You don't have permission to access [resource]. You may need to request access from your administrator."

### Data Errors
**Patterns**: "column not found", "table does not exist", "syntax error", "invalid SQL", "no such column"

**Action**: Recheck the data schema, fix the query, and retry.

- Call `tool_get_database_schema` to verify table and column names.
- Compare your query against the actual schema.
- Fix any column name typos, wrong table references, or syntax issues.
- Retry once with the corrected query.

### Rate Limiting
**Patterns**: "429", "too many requests", "rate limit exceeded", "quota exceeded"

**Action**: Inform the user and retry once.

**Response to user**: "The service is rate-limiting requests. Let me wait a moment and try again." Then retry once.

## Step 3: Communicate Clearly

When reporting an error to the user:

1. **State what happened** in plain language — no technical jargon.
2. **Explain what you tried** to fix it (if you retried).
3. **Suggest next steps** the user can take.

### Good Error Communication

"I tried to query the sales database, but the column 'total_sales' doesn't exist in the orders table. I checked the schema and found the correct column is 'order_total'. Let me rerun the query with the right column name."

"I wasn't able to access your OneDrive files — your Microsoft connection appears to have expired. Please go to Settings > Integrations and reconnect your Microsoft account, then try again."

### Bad Error Communication (Never Do This)

"Error: psycopg2.errors.UndefinedColumn: column 'total_sales' does not exist LINE 3: SELECT total_sales FROM..."

"Error 401: {"error": "invalid_token", "error_description": "The access token expired"}"

## Step 4: Recovery Failed — Suggest Alternatives

If the error cannot be resolved after one retry:

- Suggest an alternative approach (different tool, different data source).
- Ask the user if they have additional context that might help.
- If the issue is systemic (service down, account issue), clearly communicate that the user needs to take action outside the conversation.

## Critical Rules

- **NEVER retry more than once** for the same error.
- **NEVER retry authentication or permission errors** — these require user action.
- **NEVER expose raw error messages** — always translate to plain language.
- **NEVER silently ignore errors** — always inform the user about what happened.
- **NEVER guess at fixes** — if you are unsure why an error occurred, say so and ask the user.
