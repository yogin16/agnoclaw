"""
File operation tools — Read, Write, Edit, Glob, Grep.

Rules (aligned with Claude Code):
- Always read a file before editing it
- Use Edit for targeted changes; old_string must be unique in the file
- Use Write only for new files or full rewrites
- Use Glob to find files by pattern; Grep to search file contents
- Always use absolute paths
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Optional

from agno.tools import tool
from agno.tools.toolkit import Toolkit


class FilesToolkit(Toolkit):
    """All file operation tools bundled as a Toolkit."""

    def __init__(self, workspace_dir: Optional[str | Path] = None):
        super().__init__(name="files")
        self.workspace_dir = Path(workspace_dir).expanduser() if workspace_dir else Path.cwd()
        self.register(self.read_file)
        self.register(self.write_file)
        self.register(self.edit_file)
        self.register(self.glob_files)
        self.register(self.grep_files)
        self.register(self.list_dir)

    def read_file(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """
        Read the contents of a file.

        Args:
            path: Absolute path to the file.
            offset: Line number to start reading from (1-indexed, 0 = from start).
            limit: Maximum number of lines to read.

        Returns:
            File contents with line numbers, or an error message.
        """
        file_path = Path(path).expanduser()
        if not file_path.exists():
            return f"[error] File not found: {path}"
        if not file_path.is_file():
            return f"[error] Not a file: {path}"

        try:
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(0, offset - 1) if offset > 0 else 0
            end = start + limit
            selected = lines[start:end]

            # Format with line numbers (cat -n style)
            numbered = [f"{start + i + 1:6}\t{line}" for i, line in enumerate(selected)]
            result = "\n".join(numbered)

            if len(lines) > end:
                result += f"\n... ({len(lines) - end} more lines)"
            return result
        except Exception as e:
            return f"[error] Could not read {path}: {e}"

    def write_file(self, path: str, content: str) -> str:
        """
        Write content to a file (creates or overwrites).

        Args:
            path: Absolute path to the file.
            content: Content to write.

        Returns:
            Success message or error.
        """
        file_path = Path(path).expanduser()
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            lines = content.count("\n") + 1
            return f"Written {lines} lines to {path}"
        except Exception as e:
            return f"[error] Could not write {path}: {e}"

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        """
        Replace an exact string in a file. The old_string must appear exactly once.

        Read the file first before calling this tool.

        Args:
            path: Absolute path to the file.
            old_string: Exact string to replace (must be unique in the file).
            new_string: Replacement string.

        Returns:
            Success message or error (including if old_string is not unique or not found).
        """
        file_path = Path(path).expanduser()
        if not file_path.exists():
            return f"[error] File not found: {path}. Read the file first."

        try:
            content = file_path.read_text(encoding="utf-8")
            count = content.count(old_string)

            if count == 0:
                # Show context around similar content to help
                return (
                    f"[error] old_string not found in {path}.\n"
                    f"Make sure to read the file first and copy the exact text including whitespace."
                )
            if count > 1:
                return (
                    f"[error] old_string appears {count} times in {path}. "
                    f"Provide more surrounding context to make it unique."
                )

            new_content = content.replace(old_string, new_string, 1)
            file_path.write_text(new_content, encoding="utf-8")
            return f"Edited {path}: replaced 1 occurrence."
        except Exception as e:
            return f"[error] Could not edit {path}: {e}"

    def glob_files(self, pattern: str, base_dir: Optional[str] = None, path: Optional[str] = None) -> str:
        """
        Find files matching a glob pattern.

        Args:
            pattern: Glob pattern, e.g. '**/*.py', 'src/**/*.ts', '*.md'
            base_dir: Base directory to search from. Defaults to workspace.
            path: Alias for base_dir (accepted for compatibility).

        Returns:
            Newline-separated list of matching file paths, sorted by modification time.
        """
        # Accept 'path' as alias for 'base_dir' (models often use this name)
        directory = base_dir or path
        search_dir = Path(directory).expanduser() if directory else self.workspace_dir
        if not search_dir.exists():
            return f"[error] Directory not found: {search_dir}"

        try:
            matches = list(search_dir.glob(pattern))
            # Sort by modification time (newest first)
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            if not matches:
                return f"[no matches] Pattern '{pattern}' found no files in {search_dir}"
            return "\n".join(str(m) for m in matches)
        except Exception as e:
            return f"[error] Glob failed: {e}"

    def grep_files(
        self,
        pattern: str,
        path: Optional[str] = None,
        glob: Optional[str] = None,
        case_insensitive: bool = False,
        context_lines: int = 0,
        max_results: int = 50,
    ) -> str:
        """
        Search for a regex pattern in file contents.

        Args:
            pattern: Regular expression pattern to search for.
            path: File or directory to search. Defaults to workspace.
            glob: Optional glob pattern to filter files (e.g. '*.py').
            case_insensitive: Case-insensitive matching.
            context_lines: Lines of context to show before and after each match.
            max_results: Maximum number of matching lines to return.

        Returns:
            Matching lines with file:line_number:content format.
        """
        search_path = Path(path).expanduser() if path else self.workspace_dir
        flags = re.IGNORECASE if case_insensitive else 0

        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            return f"[error] Invalid regex pattern: {e}"

        results: list[str] = []
        count = 0

        def search_file(file_path: Path) -> None:
            nonlocal count
            if count >= max_results:
                return
            try:
                lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
                for i, line in enumerate(lines):
                    if compiled.search(line):
                        if context_lines > 0:
                            start = max(0, i - context_lines)
                            end = min(len(lines), i + context_lines + 1)
                            for j in range(start, end):
                                prefix = ">" if j == i else " "
                                results.append(f"{file_path}:{j + 1}{prefix} {lines[j]}")
                            results.append("---")
                        else:
                            results.append(f"{file_path}:{i + 1}: {line}")
                        count += 1
                        if count >= max_results:
                            break
            except Exception:
                pass  # Skip unreadable files

        if search_path.is_file():
            search_file(search_path)
        else:
            for file_path in search_path.rglob("*"):
                if not file_path.is_file():
                    continue
                if glob and not fnmatch.fnmatch(file_path.name, glob):
                    continue
                # Skip binary-looking files and common non-text dirs
                if any(part.startswith(".") for part in file_path.parts):
                    continue
                search_file(file_path)
                if count >= max_results:
                    break

        if not results:
            return f"[no matches] Pattern '{pattern}' not found"

        output = "\n".join(results)
        if count >= max_results:
            output += f"\n... [truncated at {max_results} matches]"
        return output

    def list_dir(self, path: Optional[str] = None) -> str:
        """
        List the contents of a directory.

        Args:
            path: Directory path. Defaults to workspace.

        Returns:
            Directory listing with file types and sizes.
        """
        dir_path = Path(path).expanduser() if path else self.workspace_dir
        if not dir_path.exists():
            return f"[error] Directory not found: {dir_path}"
        if not dir_path.is_dir():
            return f"[error] Not a directory: {dir_path}"

        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name))
            lines = []
            for entry in entries:
                if entry.is_dir():
                    lines.append(f"d  {entry.name}/")
                else:
                    size = entry.stat().st_size
                    size_str = f"{size:>8}" if size < 1024 else f"{size // 1024:>7}K"
                    lines.append(f"f {size_str}  {entry.name}")
            return f"{dir_path}:\n" + "\n".join(lines) if lines else f"{dir_path}: (empty)"
        except Exception as e:
            return f"[error] Could not list {dir_path}: {e}"
