"""
覆盖率门禁：检查生产代码总覆盖率与关键模块最低阈值
"""
import json
import sys
from pathlib import Path
from typing import Dict

THRESHOLDS: Dict[str, float] = {
    "TOTAL": 70.0,
    "strm_generator.py": 75.0,
    "proxy_app.py": 68.0,
    "webdav_backup.py": 85.0,
    "config_manager.py": 85.0,
    "fast_scan.py": 82.0,
    "database.py": 95.0,
    "api_routes.py": 70.0,
    "admin_api.py": 68.0,
    "main.py": 30.0,
}


def main() -> int:
    """
    读取 coverage JSON 并执行阈值检查

    :return int: 进程退出码
    """
    if len(sys.argv) != 2:
        print("usage: check_coverage.py <coverage.json>")
        return 2
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    files = data.get("files", {})
    failures = []
    for target, threshold in THRESHOLDS.items():
        if target == "TOTAL":
            actual = float(data.get("totals", {}).get("percent_covered") or 0.0)
        else:
            record = next(
                (
                    item
                    for name, item in files.items()
                    if name == target or name.endswith("/" + target)
                ),
                None,
            )
            actual = (
                float(record.get("summary", {}).get("percent_covered") or 0.0)
                if record
                else 0.0
            )
        status = "OK" if actual + 1e-9 >= threshold else "FAIL"
        print(f"{status} {target}: {actual:.2f}% (minimum {threshold:.2f}%)")
        if status == "FAIL":
            failures.append(target)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
