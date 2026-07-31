# Common Errors by Tool Type

## SQL Tool Errors

| Error Pattern | Cause | Recovery Action |
|---|---|---|
| `column "X" does not exist` | Wrong column name or missing table alias | Call tool_get_database_schema, find correct column name, retry |
| `relation "X" does not exist` | Wrong table name or missing schema prefix | Check schema for correct table name and schema prefix |
| `syntax error at or near "X"` | SQL syntax error | Review SQL for missing commas, unmatched parentheses, wrong keywords |
| `permission denied for table "X"` | User lacks SELECT permission | Do NOT retry. Inform user they need database access granted |
| `statement timeout` | Query too slow | Simplify query: add WHERE filters, remove unnecessary JOINs, add LIMIT |
| `division by zero` | Dividing by a column that contains zero | Wrap denominator with NULLIF: `x / NULLIF(y, 0)` |
| `invalid input syntax for type X` | Type mismatch in comparison or cast | Check column types in schema, add explicit CAST if needed |

## OneDrive Tool Errors

| Error Pattern | Cause | Recovery Action |
|---|---|---|
| `itemNotFound` | File or folder does not exist at path | List parent directory to find correct path |
| `accessDenied` | No permission to access file | Do NOT retry. Inform user to check sharing permissions |
| `invalidRequest` | Malformed path or parameters | Check path format (forward slashes, no leading slash) |
| `notAllowed` | Operation not supported on this item | Check if trying to read a folder or unsupported file type |
| `nameAlreadyExists` | Upload conflict | Ask user if they want to rename or overwrite |
| `quotaLimitReached` | OneDrive storage full | Inform user their OneDrive storage is full |
| `serviceNotAvailable` | Microsoft service outage | Retry once. If still failing, inform user to try later |

## Google Drive Tool Errors

| Error Pattern | Cause | Recovery Action |
|---|---|---|
| `404 File not found` | File ID is invalid or file was deleted | Search for the file by name instead |
| `403 The caller does not have permission` | No access to file | Do NOT retry. User needs to request access |
| `403 Rate limit exceeded` | Too many API calls | Retry once after brief pause |
| `400 Bad request` | Invalid parameters | Check file ID format and other parameters |
| `401 Invalid credentials` | Token expired | Do NOT retry. User must reconnect Google account |

## RAG / Knowledge Base Errors

| Error Pattern | Cause | Recovery Action |
|---|---|---|
| `knowledge base not found` | KB was deleted or ID is wrong | Call tool_list_knowledge to get current list |
| `indexing in progress` | KB is still processing files | Inform user to wait; check again later |
| `file type not supported` | Uploaded unsupported format | Inform user of supported types: PDF, DOCX, XLSX, CSV, TXT, MD, JSON |
| `file too large` | File exceeds size limit | Suggest splitting the file or compressing it |
| `no results found` | Query did not match any content | Reformulate with different terms, try broader search |

## S3 / Storage Errors

| Error Pattern | Cause | Recovery Action |
|---|---|---|
| `NoSuchBucket` | Bucket does not exist | Verify bucket name with user |
| `NoSuchKey` | File not found in bucket | List bucket contents to find correct key |
| `AccessDenied` | Missing IAM permissions | Do NOT retry. User needs to update permissions |
| `BucketAlreadyExists` | Bucket name taken | Suggest a different bucket name |
| `EntityTooLarge` | File exceeds upload limit | Suggest splitting or compressing the file |

## General API Errors

| HTTP Code | Meaning | Recovery Action |
|---|---|---|
| 400 | Bad Request | Fix input parameters and retry once |
| 401 | Unauthorized | Do NOT retry. User must re-authenticate |
| 403 | Forbidden | Do NOT retry. User lacks permissions |
| 404 | Not Found | Verify resource exists; check for typos in identifiers |
| 408 | Request Timeout | Retry once |
| 429 | Too Many Requests | Retry once after brief pause |
| 500 | Internal Server Error | Retry once. If persists, service has an issue |
| 502 | Bad Gateway | Retry once. Usually transient |
| 503 | Service Unavailable | Retry once. Service may be under maintenance |
| 504 | Gateway Timeout | Retry once. Consider simplifying the request |
