"""生成可直接导入 AstrBot 的 ZIP。"""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def build() -> Path:
    root = Path(__file__).resolve().parents[1]
    target = root / "dist" / "astrbot_plugin_wevote.zip"
    target.parent.mkdir(exist_ok=True)
    files = [root / name for name in ("main.py", "metadata.yaml", "_conf_schema.json", "README.md")]
    files.extend(sorted((root / "wevote").glob("*.py")))
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(root).as_posix())
    return target


if __name__ == "__main__":
    print(build())
