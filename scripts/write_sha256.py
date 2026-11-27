"""
SHA256 校验文件生成脚本：支持中文文件名并输出标准校验格式
"""
import argparse
import hashlib
from pathlib import Path


def write_sha256(file_path: Path) -> Path:
    """
    为目标文件生成 SHA256 校验文件

    :param file_path (Path): 待校验文件路径

    :return Path: 校验文件路径
    """
    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
    checksum_path = file_path.with_name(file_path.name + ".sha256")
    checksum_path.write_text(f"{digest}  {file_path.name}\n", encoding="utf-8")
    return checksum_path


def main() -> int:
    """
    脚本入口

    :return int: 进程退出码
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    args = parser.parse_args()
    write_sha256(Path(args.file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
