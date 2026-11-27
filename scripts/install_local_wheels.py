"""
本地 wheel 安装器：兼容当前平台的必需 wheel 失败即中止，可选 wheel 允许跳过
"""
import subprocess
import sys
from pathlib import Path

try:
    from packaging.tags import sys_tags
    from packaging.utils import parse_wheel_filename
except ImportError:
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.utils import parse_wheel_filename


OPTIONAL_WHEELS = frozenset({"full-strm-sync"})


def install_local_wheels(wheel_dir: Path) -> int:
    """
    安装仓库锁包，当前平台不兼容的 wheel 与安装失败的可选 wheel 可跳过

    :param wheel_dir (Path): wheel 所在目录

    :return int: 进程退出码
    """
    supported_tags = set(sys_tags())
    for wheel in sorted(wheel_dir.glob("*.whl")):
        name, _version, _build, tags = parse_wheel_filename(wheel.name)
        if tags.isdisjoint(supported_tags):
            print(f"SKIP platform: {wheel.name}")
            continue
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--find-links",
                str(wheel_dir),
                str(wheel),
            ],
            capture_output=True,
            text=True,
        )
        normalized_name = str(name).replace("_", "-").lower()
        if result.returncode == 0:
            print(f"OK: {wheel.name}")
            continue
        if normalized_name in OPTIONAL_WHEELS:
            print(f"SKIP optional: {wheel.name}")
            print(result.stdout)
            print(result.stderr)
            continue
        print(f"FAILED required wheel: {wheel.name}")
        print(result.stdout)
        print(result.stderr)
        return result.returncode or 1
    return 0


def main() -> int:
    """
    脚本入口

    :return int: 进程退出码
    """
    wheel_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("combined/wheels")
    return install_local_wheels(wheel_dir)


if __name__ == "__main__":
    raise SystemExit(main())
