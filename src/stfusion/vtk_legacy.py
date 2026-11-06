"""解析传统 VTK STRUCTURED_POINTS（ASCII 头 + 二进制场），避免把 GB 级切片整文件读进内存。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

_DTYPE_SIZES = {
    "bit": 1,
    "unsigned_char": 1,
    "char": 1,
    "unsigned_short": 2,
    "short": 2,
    "unsigned_int": 4,
    "int": 4,
    "unsigned_long": 8,
    "long": 8,
    "float": 4,
    "double": 8,
}


class VtkLegacyError(ValueError):
    pass


def _read_ascii_line(handle) -> str:
    raw = handle.readline()
    if not raw:
        return ""
    return raw.decode("ascii", errors="replace").strip()


def parse_structured_points(path: Path) -> dict[str, Any]:
    """读取 STRUCTURED_POINTS 头和各场的文件偏移。不加载场数据。"""
    path = Path(path)
    info: dict[str, Any] = {
        "path": path,
        "dataset": None,
        "dimensions": None,
        "origin": None,
        "spacing": None,
        "n_points": None,
        "fields": [],
        "binary": False,
    }
    with path.open("rb") as handle:
        version = _read_ascii_line(handle)
        if not version.lower().startswith("# vtk"):
            raise VtkLegacyError(f"不是 VTK 文件: {path}")
        _read_ascii_line(handle)  # title
        fmt = _read_ascii_line(handle).upper()
        info["binary"] = fmt.startswith("BINARY")
        dataset = _read_ascii_line(handle)
        if "STRUCTURED_POINTS" not in dataset.upper():
            raise VtkLegacyError(f"不是 STRUCTURED_POINTS: {path}")
        info["dataset"] = "STRUCTURED_POINTS"

        while True:
            pos = handle.tell()
            line = _read_ascii_line(handle)
            if not line:
                break
            key = line.split()[0].upper()
            if key == "DIMENSIONS":
                parts = line.split()
                info["dimensions"] = (int(parts[1]), int(parts[2]), int(parts[3]))
            elif key == "ORIGIN":
                parts = line.split()
                info["origin"] = (float(parts[1]), float(parts[2]), float(parts[3]))
            elif key == "SPACING":
                parts = line.split()
                info["spacing"] = (float(parts[1]), float(parts[2]), float(parts[3]))
            elif key == "POINT_DATA":
                info["n_points"] = int(line.split()[1])
            elif key == "CELL_DATA":
                break
            elif key == "FIELD":
                n_fields = int(line.split()[-1])
                info["fields"].extend(_parse_field_block(handle, n_fields, info["binary"]))
                break
            elif key in {"SCALARS", "VECTORS", "NORMALS", "TENSORS", "COLOR_SCALARS"}:
                handle.seek(pos)
                info["fields"].extend(_parse_named_arrays(handle, info["n_points"], info["binary"]))
                break

    dims = info["dimensions"]
    origin = info["origin"]
    spacing = info["spacing"]
    if not dims or not origin or not spacing:
        raise VtkLegacyError(f"VTK 头缺少 DIMENSIONS/ORIGIN/SPACING: {path}")
    if info["n_points"] is None:
        info["n_points"] = int(dims[0] * dims[1] * dims[2])
    nx, ny, nz = dims
    dx, dy, dz = spacing
    ox, oy, oz = origin
    info["bounds"] = (
        ox,
        ox + (nx - 1) * dx,
        oy,
        oy + (ny - 1) * dy,
        oz,
        oz + (nz - 1) * dz,
    )
    return info


def _parse_field_block(handle, n_fields: int, binary: bool) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for _ in range(n_fields):
        line = _read_ascii_line(handle)
        while line == "":
            line = _read_ascii_line(handle)
            if not line:
                return fields
        parts = line.split()
        if len(parts) < 4:
            raise VtkLegacyError(f"无法解析 FIELD 行: {line}")
        name, ncomp, nvals, dtype = parts[0], int(parts[1]), int(parts[2]), parts[3].lower()
        offset = handle.tell()
        nbytes = nvals * ncomp * _DTYPE_SIZES.get(dtype, 4)
        fields.append(
            {
                "name": name,
                "ncomp": ncomp,
                "nvals": nvals,
                "dtype": dtype,
                "offset": offset,
            }
        )
        if binary:
            handle.seek(offset + nbytes)
            peek = handle.read(1)
            if peek not in {b"", b"\n"}:
                handle.seek(-1, 1)
        else:
            raise VtkLegacyError("目前只支持 BINARY FIELD")
    return fields


def _parse_named_arrays(handle, n_points: int | None, binary: bool) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    while True:
        pos = handle.tell()
        line = _read_ascii_line(handle)
        if not line:
            break
        key = line.split()[0].upper()
        if key == "SCALARS":
            parts = line.split()
            name = parts[1]
            dtype = parts[2].lower() if len(parts) > 2 else "float"
            ncomp = int(parts[3]) if len(parts) > 3 else 1
            nxt = _read_ascii_line(handle)
            if not nxt.upper().startswith("LOOKUP_TABLE"):
                handle.seek(pos)
                break
            offset = handle.tell()
        elif key == "VECTORS":
            parts = line.split()
            name = parts[1]
            dtype = parts[2].lower() if len(parts) > 2 else "float"
            ncomp = 3
            offset = handle.tell()
        elif key == "FIELD":
            fields.extend(_parse_field_block(handle, int(line.split()[-1]), binary))
            continue
        elif key == "CELL_DATA":
            break
        else:
            break
        if n_points is None:
            break
        nbytes = n_points * ncomp * _DTYPE_SIZES.get(dtype, 4)
        fields.append(
            {
                "name": name,
                "ncomp": ncomp,
                "nvals": n_points,
                "dtype": dtype,
                "offset": offset,
            }
        )
        if not binary:
            break
        handle.seek(offset + nbytes)
        peek = handle.read(1)
        if peek not in {b"", b"\n"}:
            handle.seek(-1, 1)
    return fields


def memmap_field(path: Path, field: dict[str, Any], dimensions: tuple[int, int, int]) -> np.memmap:
    nx, ny, nz = dimensions
    dtype = ">f8" if field["dtype"] == "double" else ">f4"
    return np.memmap(
        path,
        dtype=dtype,
        mode="r",
        offset=int(field["offset"]),
        shape=(nz, ny, nx),
    )


def write_structured_points_fields(
    path: Path,
    origin: tuple[float, float, float],
    spacing: tuple[float, float, float],
    dimensions: tuple[int, int, int],
    fields: dict[str, np.ndarray],
) -> None:
    """写入与 v10 切片相同的 BINARY FIELD VTK，便于测试。数组形状为 (nz, ny, nx)。"""
    nx, ny, nz = dimensions
    n = nx * ny * nz
    names = list(fields)
    header = (
        "# vtk DataFile Version 5.1\n"
        "vtk output\n"
        "BINARY\n"
        "DATASET STRUCTURED_POINTS\n"
        f"DIMENSIONS {nx} {ny} {nz}\n"
        f"SPACING {spacing[0]} {spacing[1]} {spacing[2]}\n"
        f"ORIGIN {origin[0]} {origin[1]} {origin[2]}\n"
        f"POINT_DATA {n}\n"
        f"FIELD FieldData {len(names)}\n"
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(header.encode("ascii"))
        for i, name in enumerate(names):
            arr = np.asarray(fields[name], dtype=np.float32)
            if arr.shape != (nz, ny, nx):
                raise VtkLegacyError(f"{name} 形状应为 {(nz, ny, nx)}，实际 {arr.shape}")
            if i:
                handle.write(b"\n")
            handle.write(f"{name} 1 {n} float\n".encode("ascii"))
            handle.write(np.ascontiguousarray(arr, dtype=">f4").tobytes())
