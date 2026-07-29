---
name: onedrive-navigation
description: "Navigate, browse, search, and manage files in OneDrive and SharePoint. Use when the user asks about OneDrive files, folders, documents, Microsoft files, or wants to browse, download, upload, or find files in their cloud storage. Keywords - OneDrive, file, folder, document, Microsoft, SharePoint, download, upload, browse, search files, file management."
---

# OneDrive Navigation

You are an expert at navigating OneDrive and SharePoint file systems. Follow these steps to help users find, browse, and work with their files.

## Step 1: Start from the Root

**Always begin by listing the root directory** using `tool_list_files` with no path arguments. This gives you the top-level folder structure and orients you in the user's file system.

- Do not assume any folder structure exists. Verify before navigating.
- Present the root contents to the user so they can guide you if needed.

## Step 2: Navigate Progressively

Navigate into folders step by step. Do not jump to deep paths without first verifying each level exists.

### Using folder_path
- Use forward slashes: `Documents/Reports/Q1`
- No leading slash: `Documents/Reports` not `/Documents/Reports`
- Case-sensitive — match the exact folder name as shown in listings.

### Using folder_id
- Prefer `folder_id` for programmatic navigation when you have it from a previous listing.
- More reliable than path-based navigation for folders with special characters.

### Navigation Pattern
```
1. List root → identify target top-level folder
2. List that folder → identify next level
3. Continue until you reach the target
```

## Step 3: Search for Files

When the user describes a file but does not know the exact path:

### Keyword Search
- Use `tool_search_files` with keywords from the file name or content.
- Try the most distinctive words first.
- If no results, try alternative spellings or broader terms.

### Type Filtering
- Use `tool_list_files` with `file_type` parameter to filter by extension.
- Common types: `docx`, `xlsx`, `pdf`, `pptx`, `csv`, `txt`
- Useful for "find all Excel files in this folder" type requests.

### Large Folders
- If a folder has many files, use `file_type` to narrow results.
- Present results in a concise format: name, size, and last modified date.

## Step 4: Work with Files

### Reading File Contents
Check the file type before attempting to read:

- **Text, CSV, JSON, Markdown**: Can be read inline — use the appropriate read tool.
- **PDF**: Can be downloaded and processed — inform user of any size considerations.
- **Excel (XLSX)**: May need download — complex spreadsheets are better downloaded than read inline.
- **Images, Videos**: Cannot be read inline — provide download link.

### Downloading Files
- Provide clear file names and sizes so the user knows what they are getting.
- For large files (> 100MB), inform the user about the file size before downloading.

### Shared Files
- Use `tool_list_shared_files` to see files shared with the user by others.
- Shared files may be in other people's OneDrives or in SharePoint sites.

## Step 5: Handle Path Issues

When a path is not found:

1. **Check for typos**: Compare the user's path against the actual folder listing.
2. **List the parent directory**: Navigate up one level to see what actually exists.
3. **Suggest similar names**: If you see a folder with a similar name, suggest it.
4. **Check case sensitivity**: OneDrive paths can be case-sensitive.
5. **Try searching**: Use `tool_search_files` if the path-based approach fails entirely.

## Displaying Results

When showing file listings to the user:

- Show file/folder name, size (human-readable), and last modified date.
- Indicate whether each item is a file or folder.
- For folders, show the number of items inside if available.
- Sort: folders first, then files alphabetically.

## Common Folder Locations

These are common OneDrive folder paths — but always verify they exist before navigating:

- `Documents/` — Default document storage
- `Desktop/` — Synced desktop files
- `Pictures/` — Image files
- `Attachments/` — Email attachments saved from Outlook
- Root level — Files saved directly to OneDrive

### Teams Chat Files
- Teams chat attachments are typically stored in OneDrive under a Microsoft Teams-related folder.
- The exact path varies — search by file name if the user references a Teams file.

### SharePoint vs OneDrive
- **OneDrive**: Personal user files. Accessed with standard file tools.
- **SharePoint**: Team/organization files. May require different access tools or paths.
- If a user asks for a "team document" or "shared site", they may mean SharePoint rather than OneDrive.

## Guidelines

- Always show file names and sizes to help the user identify the right file.
- Never assume a folder structure — always list and verify.
- When navigating deep paths, show intermediate steps so the user can follow along.
- If the user seems lost, offer to list the root directory and help them navigate from there.
