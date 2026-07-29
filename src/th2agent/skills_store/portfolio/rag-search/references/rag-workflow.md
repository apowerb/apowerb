# RAG Workflow Reference

## Complete Workflow

### 1. Knowledge Base Creation Flow

```
User uploads files
    ↓
Call tool_create_knowledge(name, files)
    ↓
Wait for indexing status → "ready"
    ↓
Knowledge base available for search
```

### 2. Search Flow

```
User asks a question
    ↓
Call tool_list_knowledge → identify relevant KB
    ↓
Reformulate question → targeted search query
    ↓
Call tool_search_knowledge(kb_id, query)
    ↓
Review results → relevant?
    ├── Yes → Extract answer, cite source
    └── No → Reformulate query and retry (max 2 times)
              ↓
              Still no results → Inform user
```

## Query Reformulation Strategies

### Strategy 1: Synonym Expansion
Replace key terms with synonyms or related terms.
- "profit" → "revenue", "earnings", "income", "net income"
- "employee" → "staff", "worker", "team member", "personnel"
- "policy" → "guideline", "rule", "procedure", "protocol"

### Strategy 2: Specificity Adjustment
If the first query is too broad, narrow it. If too narrow, broaden it.

**Too broad**: "company performance"
**Narrowed**: "Q3 2025 revenue growth percentage year-over-year"

**Too narrow**: "John Smith's PTO balance on March 15"
**Broadened**: "PTO balance leave allocation employee"

### Strategy 3: Entity Focus
Extract the key entity (person, product, date, metric) and search for it directly.

**Question**: "What was the outcome of the board meeting in January?"
**Entity-focused query**: "board meeting January 2025 decisions outcomes resolutions"

### Strategy 4: Structural Terms
Include document structure terms that may help locate information.
- "summary", "conclusion", "table of contents"
- "section", "chapter", "appendix"
- "definition", "glossary", "terms"

## Handling Different File Types

### PDFs
- May contain scanned images — OCR quality varies.
- Tables in PDFs may not be perfectly extracted. If data looks garbled, inform the user.
- Multi-column layouts may split text oddly — reformulate if initial search misses.

### Word Documents (DOCX)
- Headers, footers, and text boxes may be indexed separately.
- Track changes and comments are typically not included in the index.

### Excel / CSV Files
- Column headers become searchable terms.
- Search by column name + expected value for best results.
- Numeric-only cells may not match keyword searches well.

### Markdown / Text Files
- Best indexing quality — text is clean and structured.
- Use heading text as search terms when looking for specific sections.

## Multi-Document Search

When searching across multiple knowledge bases:

1. Search each knowledge base separately.
2. Compare relevance scores across results.
3. Synthesize findings, noting which document each piece of information comes from.
4. If documents contradict each other, present both perspectives and note the discrepancy.

## Error Handling

| Error | Action |
|---|---|
| Knowledge base not found | Call tool_list_knowledge to refresh the list |
| Indexing in progress | Wait and inform the user |
| No results returned | Try reformulated query (max 2 attempts) |
| Low relevance scores | Broaden the query or try different terms |
| File upload failed | Check file type is supported, check file size |
