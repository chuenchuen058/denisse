---
name: organize_files
description: How to help organize, rename, or clean up files in a folder
---

# Organizing files

When asked to organize, clean up, or rename files in a folder:

1. Use `list_directory` first to see what's actually there. Never guess
   filenames.
2. Group by what you observe (file type, date in the filename, naming
   pattern) rather than assuming a scheme.
3. Propose the plan in your reply BEFORE moving/renaming anything, unless
   the user's request was already extremely specific (e.g. "rename x.txt to
   y.txt").
4. Use `run_shell_command` for the actual move/rename (e.g. `Move-Item`,
   `Rename-Item` in PowerShell). Prefer moving into clearly named
   subfolders over deleting or overwriting anything.
5. Never delete files. If cleanup implies removal, move things to a
   `_to_review` subfolder instead and tell the user, so nothing is lost.
6. After acting, use `list_directory` again to confirm the result and
   report back what changed.
