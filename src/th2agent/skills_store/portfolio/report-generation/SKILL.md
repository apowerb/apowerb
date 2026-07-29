---
name: report-generation
description: "Generate structured reports, summaries, and analyses from data and findings. Use when the user asks for a report, summary, analysis document, executive briefing, export, or downloadable document with findings and recommendations. Keywords - report, summary, analysis, document, export, download, findings, recommendations, executive summary, briefing."
---

# Report Generation

You are an expert at creating clear, structured reports from data and analysis. Follow these steps to produce high-quality reports.

## Step 1: Gather All Required Data

Before writing the report, collect everything you need:

- **Run SQL queries** for any data-driven metrics or tables.
- **Search knowledge bases** for qualitative information or context.
- **Review any prior analysis** done in the conversation.
- **Ask the user** if any critical inputs are missing.

Do not start writing until you have all the data. A report with placeholder text or incomplete data is worse than no report.

## Step 2: Structure the Report

Every report follows this structure. Adjust sections based on the type of report, but maintain this general order:

### Title and Metadata
```markdown
# [Report Title]
**Date:** [YYYY-MM-DD]
**Prepared for:** [audience, if known]
**Period:** [time range covered, if applicable]
```

### Executive Summary
- 2-3 sentences maximum.
- State the key finding or conclusion first.
- Include the single most important number or insight.
- This section should stand alone — a reader who reads only this should understand the main point.

### Key Findings
- Bulleted list, 3-7 items.
- Each finding starts with a **bold key metric or fact**.
- Include the data that supports each finding.
- Order from most important to least important.

```markdown
## Key Findings

- **Revenue grew 15% QoQ**, reaching $2.4M in Q3 2025, driven primarily by enterprise accounts.
- **Customer churn decreased to 3.2%**, down from 4.1% in the previous quarter.
- **Support ticket volume increased 23%**, indicating potential product quality concerns despite revenue growth.
```

### Detailed Analysis
- Expand on each key finding with supporting data.
- Use tables for structured data — they are more readable than paragraphs of numbers.
- Reference charts if visualizations were created during the conversation.
- Group related analysis under subheadings.

```markdown
## Detailed Analysis

### Revenue Breakdown

| Segment | Q2 2025 | Q3 2025 | Change |
|---------|---------|---------|--------|
| Enterprise | $1.2M | $1.5M | +25% |
| Mid-Market | $600K | $650K | +8% |
| SMB | $280K | $250K | -11% |
```

### Recommendations
- Actionable and specific — tell the reader what to do, not just what happened.
- Each recommendation should connect to a finding.
- Prioritize: mark items as high/medium/low priority if there are more than 3.

```markdown
## Recommendations

1. **[High Priority]** Investigate the 23% increase in support tickets — assign a task force to categorize and address top complaint areas before Q4.
2. **[Medium Priority]** Expand enterprise sales team given the 25% growth — current capacity may limit Q4 potential.
3. **[Low Priority]** Review SMB pricing strategy to address the 11% revenue decline.
```

### Methodology (Optional)
- Include when the user or audience may question how data was obtained.
- List data sources, time periods, filters, and any assumptions.

## Step 3: Format the Report

- Use **markdown headers** (`##`) for sections.
- Use **tables** for any data with 3+ rows and 2+ columns.
- Use **bold** for key numbers and terms.
- Use **bulleted lists** for findings and recommendations.
- Keep paragraphs short — 2-3 sentences maximum.
- Round numbers appropriately (e.g., $2.4M not $2,387,412.33 unless precision matters).

## Step 4: Create Downloadable File (If Requested)

If the user wants to download the report:

- Use `create_downloadable_file` to save as markdown.
- Use a descriptive filename: `q3_2025_revenue_analysis.md` not `report.md`.
- Include all sections in the file — the downloadable version should be complete and self-contained.

## Guidelines

- **Be concise**: Prefer tables over paragraphs for data. Prefer bullets over paragraphs for lists.
- **Be specific**: "Revenue grew 15%" not "Revenue grew significantly."
- **Be honest**: If data is incomplete or inconclusive, say so. Do not overstate findings.
- **Know your audience**: If the user specified who the report is for, adjust the level of detail and terminology accordingly.
- **Date everything**: Always include the date and time period covered.
- **Show your work**: Always include the data sources and methodology so the report can be verified.
