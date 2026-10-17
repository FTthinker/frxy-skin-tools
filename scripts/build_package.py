"""Build an installable add-on ZIP using only the repository's package files."""
from pathlib import Path
import ast
import hashlib
import zipfile

root = Path(__file__).resolve().parents[1]
package = root / "neox_skin_exporter"
output = root / "dist" / "NeoX_皮肤导出插件_v0.5.0.zip"
files = sorted(p for p in package.rglob("*")
               if p.is_file() and "__pycache__" not in p.parts
               and p.suffix not in {".pyc", ".pyo"})
if not (package / "__init__.py").is_file():
    raise SystemExit("Missing add-on package")
for path in files:
    if path.suffix == ".py":
        ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
output.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in files:
        archive.write(path, path.relative_to(root).as_posix())
with zipfile.ZipFile(output) as archive:
    if archive.testzip() is not None:
        raise SystemExit("ZIP integrity check failed")
digest = hashlib.sha256(output.read_bytes()).hexdigest()
(output.parent / "SHA256SUMS.txt").write_text(
    f"{digest}  {output.name}\n", encoding="utf-8")
print(f"Built {output.name}: {len(files)} files, SHA256 {digest}")
