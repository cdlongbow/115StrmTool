"""
HTML 内联 JavaScript 语法检查器：逐段调用 node --check
"""
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import List


class _ScriptParser(HTMLParser):
    """收集 HTML 中的内联脚本"""

    def __init__(self) -> None:
        super().__init__()
        self.scripts: List[str] = []
        self._in_script = False
        self._chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        """记录脚本开始标签"""
        if tag.lower() == "script" and not dict(attrs).get("src"):
            self._in_script = True
            self._chunks = []

    def handle_data(self, data: str) -> None:
        """累计脚本正文"""
        if self._in_script:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        """保存脚本正文"""
        if tag.lower() == "script" and self._in_script:
            self.scripts.append("".join(self._chunks))
            self._in_script = False
            self._chunks = []


def check_inline_js(html_path: Path) -> int:
    """
    检查 HTML 中全部内联 JavaScript 的语法

    :param html_path (Path): HTML 文件路径

    :return int: 进程退出码
    """
    parser = _ScriptParser()
    parser.feed(html_path.read_text(encoding="utf-8"))
    if not parser.scripts:
        print(f"No inline scripts found: {html_path}")
        return 0
    for index, script in enumerate(parser.scripts, start=1):
        result = subprocess.run(
            ["node", "--check"],
            input=script,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            print(f"Syntax error in {html_path} script block {index}")
            print(result.stdout)
            print(result.stderr)
            return result.returncode or 1
    print(f"OK: {len(parser.scripts)} inline script block(s)")
    return 0


def main() -> int:
    """
    脚本入口

    :return int: 进程退出码
    """
    html_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("combined/web/index.html")
    return check_inline_js(html_path)


if __name__ == "__main__":
    raise SystemExit(main())
