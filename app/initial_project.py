"""initial_project.py — 内置的初始 C 项目文件（磁盘为唯一初始来源）"""

from pathlib import Path

from .builder import HEADER_FILES, PROJECT_FILES, SOURCE_FILES

_PROJECT_ROOT = Path(__file__).resolve().parent / "c_project"


def load_initial_files() -> dict[str, bytes]:
    """从内置 c_project 目录读取固定文件的初始内容。"""
    files: dict[str, bytes] = {}
    for rel in PROJECT_FILES:
        files[rel] = (_PROJECT_ROOT / rel).read_bytes()
    return files


__all__ = ["load_initial_files", "PROJECT_FILES", "SOURCE_FILES", "HEADER_FILES"]
