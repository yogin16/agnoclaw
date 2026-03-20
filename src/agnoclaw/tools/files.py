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

import logging
from pathlib import Path
from typing import Optional

from agno.tools.toolkit import Toolkit

from .backends import LocalWorkspaceAdapter, WorkspaceAdapter

logger = logging.getLogger("agnoclaw.tools.files")


class FilesToolkit(Toolkit):
    """All file operation tools bundled as a Toolkit."""

    def __init__(
        self,
        workspace_dir: Optional[str | Path] = None,
        adapter: WorkspaceAdapter | None = None,
    ):
        super().__init__(name="files")
        self.adapter = adapter or LocalWorkspaceAdapter(workspace_dir=workspace_dir)
        self.workspace_dir = (
            Path(workspace_dir).expanduser().resolve()
            if workspace_dir is not None
            else getattr(self.adapter, "workspace_dir", Path.cwd().resolve())
        )
        self.register(self.read_file)
        self.register(self.write_file)
        self.register(self.edit_file)
        self.register(self.multi_edit_file)
        self.register(self.glob_files)
        self.register(self.grep_files)
        self.register(self.list_dir)

    def read_file(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        return self.adapter.read_file(path=path, offset=offset, limit=limit)

    def write_file(self, path: str, content: str) -> str:
        return self.adapter.write_file(path=path, content=content)

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        return self.adapter.edit_file(path=path, old_string=old_string, new_string=new_string)

    def multi_edit_file(self, path: str, edits: list) -> str:
        return self.adapter.multi_edit_file(path=path, edits=edits)

    def glob_files(self, pattern: str, base_dir: Optional[str] = None, path: Optional[str] = None) -> str:
        return self.adapter.glob_files(pattern=pattern, base_dir=base_dir, path=path)

    def grep_files(
        self,
        pattern: str,
        path: Optional[str] = None,
        glob: Optional[str] = None,
        case_insensitive: bool = False,
        context_lines: int = 0,
        max_results: int = 50,
    ) -> str:
        return self.adapter.grep_files(
            pattern=pattern,
            path=path,
            glob=glob,
            case_insensitive=case_insensitive,
            context_lines=context_lines,
            max_results=max_results,
        )

    def list_dir(self, path: Optional[str] = None) -> str:
        return self.adapter.list_dir(path=path)
