# OneDrive Path Patterns Reference

## Path Format Rules

- Use forward slashes: `Documents/Reports/Q1`
- No leading slash: `Documents/Reports` (not `/Documents/Reports`)
- No trailing slash: `Documents/Reports` (not `Documents/Reports/`)
- Case-sensitive: Match exact casing from directory listings
- Spaces are allowed: `My Documents/Annual Reports`
- Special characters: Most are allowed, but avoid `# % & { } \ < > * ? / $ ! ' " : @ + |`

## Common OneDrive Folder Structures

### Personal OneDrive
```
root/
├── Documents/
│   ├── Personal/
│   ├── Work/
│   └── Templates/
├── Desktop/
├── Pictures/
│   ├── Camera Roll/
│   └── Screenshots/
├── Downloads/
├── Attachments/
└── Notebooks/
```

### Business OneDrive
```
root/
├── Documents/
│   ├── Projects/
│   ├── Reports/
│   ├── Contracts/
│   └── Presentations/
├── Shared with me/
├── Microsoft Teams Chat Files/
└── Notebooks/
```

## Teams Chat Files Location

Teams chat file attachments are stored in the user's OneDrive at:
```
Microsoft Teams Chat Files/
```

This folder is at the root level. Files are named as uploaded — there are no subfolders by conversation or date.

To find a specific Teams file:
1. List `Microsoft Teams Chat Files/` folder
2. If too many files, search by filename using `tool_search_files`
3. Sort by modified date if the user mentions when it was shared

## Teams Channel Files

Teams channel files are stored in **SharePoint**, not OneDrive:
- Site: `sites/{team-name}`
- Library: `Shared Documents/{channel-name}/`

These require SharePoint-specific access and may not be available via standard OneDrive tools.

## SharePoint vs OneDrive Paths

### OneDrive Paths
```
Documents/Reports/Q3.xlsx
Desktop/notes.txt
Microsoft Teams Chat Files/proposal.pdf
```

### SharePoint Paths
```
sites/Marketing/Shared Documents/Campaigns/spring-2025.pptx
sites/Engineering/Shared Documents/General/architecture.pdf
```

Key differences:
- SharePoint paths start with `sites/{site-name}`
- SharePoint document libraries are typically named `Shared Documents`
- Channel folders map to channel names under `Shared Documents`

## File Type Patterns

### Filtering by Type

When using `file_type` parameter for filtering:

| User Request | file_type Value |
|---|---|
| "Excel files" | `xlsx` |
| "Word documents" | `docx` |
| "PDFs" | `pdf` |
| "PowerPoint" | `pptx` |
| "Spreadsheets" | `xlsx` (also consider `csv`) |
| "Images" | `jpg`, `png`, `gif` (search multiple types) |
| "Text files" | `txt` |

### Read vs Download Decision

| Extension | Read Inline | Download |
|---|---|---|
| txt, csv, json, md | Yes | Optional |
| docx | Limited | Recommended for complex formatting |
| xlsx | Limited | Recommended for formulas/charts |
| pdf | Yes (text extraction) | Recommended for images/layout |
| pptx | No | Required |
| jpg, png, gif | No | Required |
| mp4, mov | No | Required |

## Navigation Troubleshooting

### Common Issues

| Symptom | Likely Cause | Solution |
|---|---|---|
| "Item not found" | Wrong path or deleted file | List parent directory |
| Empty folder listing | Folder exists but is empty | Confirm with user |
| Unexpected files | Looking at wrong folder | Navigate from root to verify |
| "Access denied" | File is in another user's OneDrive | Check "Shared with me" or request access |
| Path works in browser but not in tool | URL encoding differences | Use folder_id instead of path |
