"""把风切变 / 湍流 VTK 切片采样到气象网格上。

v10 切片是 4m STRUCTURED_POINTS，场名为 HorizontalShear、VerticalShear、Epsilon、AirMask。
气象 VTK 保持原网格；分辨率成整数倍且原点对齐时，最近邻即共点抽样。
切片之间按 1 个格点重叠拼接；VTK 头里 ORIGIN 只有 6 位有效数字，拼接时用尺寸链校正。

单个切片约 1GB，而 64m 气象网格只会用到其中极少的格点。网页端先把各切片的头发给
plan_turbulence_subsets，按返回的字节区间只上传用到的行（子集目录见 SUBSET_MANIFEST），
不必上传整个切片。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pyvista as pv

from stfusion.models import TargetGrid, TileSubset, TurbulenceSource, TurbulenceTile, VtkSource
from stfusion.vtk_legacy import VtkLegacyError, memmap_field, parse_structured_points

logger = logging.getLogger(__name__)

DEFAULT_FIELDS = ("HorizontalShear", "VerticalShear", "Epsilon", "AirMask")
VTK_SUFFIXES = {".vtk", ".vti"}
SUBSET_MANIFEST = "turbulence_subset.json"
SUBSET_DTYPES = {"float": ">f4", "double": ">f8"}


class TurbulenceError(ValueError):
    pass


def discover_turbulence_files(path: Path) -> list[Path]:
    path = Path(path)
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise TurbulenceError(f"找不到风切变/湍流路径: {path}")
    files = sorted(
        p
        for p in path.iterdir()
        if p.is_file() and p.suffix.lower() in VTK_SUFFIXES
    )
    if not files:
        raise TurbulenceError(f"目录中没有 VTK 切片: {path}")
    return files


def _cluster_ids(values: np.ndarray, tol: float) -> np.ndarray:
    order = np.argsort(values)
    labels = np.empty(len(values), dtype=np.int32)
    current = 0
    prev = float(values[order[0]])
    for idx in order:
        val = float(values[idx])
        if val - prev > tol:
            current += 1
            prev = val
        labels[idx] = current
    return labels


def refine_tile_origins(tiles: list[TurbulenceTile]) -> None:
    """用西南角切片 + 各行/列格点数，把 ORIGIN 校正到间距的整数倍。"""
    if len(tiles) < 2:
        return
    xs = np.array([t.origin[0] for t in tiles], dtype=np.float64)
    ys = np.array([t.origin[1] for t in tiles], dtype=np.float64)
    dx = float(np.median([t.spacing[0] for t in tiles]))
    dy = float(np.median([t.spacing[1] for t in tiles]))
    tol_x = max(abs(dx) * 2.5, 8.0)
    tol_y = max(abs(dy) * 2.5, 8.0)
    col = _cluster_ids(xs, tol_x)
    row = _cluster_ids(ys, tol_y)
    ncols = int(col.max()) + 1
    nrows = int(row.max()) + 1
    if ncols * nrows == 1:
        return

    nx_col = []
    for c in range(ncols):
        members = [t for t, cid in zip(tiles, col) if cid == c]
        if not members:
            raise TurbulenceError("切片在 X 方向有空缺列，无法拼接 ORIGIN")
        nx_col.append(members[0].dimensions[0])
    ny_row = []
    for r in range(nrows):
        members = [t for t, rid in zip(tiles, row) if rid == r]
        if not members:
            raise TurbulenceError("切片在 Y 方向有空缺行，无法拼接 ORIGIN")
        ny_row.append(members[0].dimensions[1])

    x0 = float(min(t.origin[0] for t, cid in zip(tiles, col) if cid == 0))
    y0 = float(min(t.origin[1] for t, rid in zip(tiles, row) if rid == 0))
    x_origins = [x0]
    for c in range(1, ncols):
        x_origins.append(x_origins[-1] + (nx_col[c - 1] - 1) * dx)
    y_origins = [y0]
    for r in range(1, nrows):
        y_origins.append(y_origins[-1] + (ny_row[r - 1] - 1) * dy)

    for tile, cid, rid in zip(tiles, col, row):
        ox, oy, oz = tile.origin
        tile.origin = (x_origins[int(cid)], y_origins[int(rid)], oz)


def tile_from_header(
    path: Path,
    header: dict[str, Any],
    field_filter: list[str] | None = None,
) -> TurbulenceTile | None:
    available = {item["name"]: item for item in header["fields"]}
    if field_filter:
        names = [name for name in field_filter if name in available]
    else:
        names = [name for name in DEFAULT_FIELDS if name in available]
        if not names:
            names = list(available)
    if not names:
        return None
    return TurbulenceTile(
        path=Path(path),
        origin=tuple(header["origin"]),
        spacing=tuple(header["spacing"]),
        dimensions=tuple(header["dimensions"]),
        n_points=int(header["n_points"]),
        fields={name: available[name] for name in names},
    )


def load_turbulence_tiles(path: Path, field_filter: list[str] | None = None) -> list[TurbulenceTile]:
    path = Path(path)
    if path.is_dir() and (path / SUBSET_MANIFEST).is_file():
        return load_subset_tiles(path, field_filter)
    files = discover_turbulence_files(path)
    tiles: list[TurbulenceTile] = []
    for file in files:
        try:
            header = parse_structured_points(file)
        except VtkLegacyError as exc:
            logger.warning("跳过无法解析的 VTK: %s (%s)", file.name, exc)
            continue
        tile = tile_from_header(file, header, field_filter)
        if tile is None:
            logger.warning("切片没有可用标量场: %s", file.name)
            continue
        tiles.append(tile)
    if not tiles:
        raise TurbulenceError(f"没有可读的风切变/湍流切片: {path}")
    refine_tile_origins(tiles)
    return tiles


def header_from_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """校验网页端解析出的切片头（结构同 parse_structured_points 的返回值）。"""
    try:
        dims = tuple(int(v) for v in raw["dimensions"])
        origin = tuple(float(v) for v in raw["origin"])
        spacing = tuple(float(v) for v in raw["spacing"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TurbulenceError(f"切片头缺少 dimensions/origin/spacing: {exc}") from exc
    if len(dims) != 3 or len(origin) != 3 or len(spacing) != 3:
        raise TurbulenceError("切片头 dimensions/origin/spacing 必须各有 3 个分量")
    if min(dims) < 1 or any(v == 0 or not np.isfinite(v) for v in spacing):
        raise TurbulenceError(f"切片头尺寸或间距无效: {dims} / {spacing}")
    n_points = int(np.prod(dims, dtype=np.int64))
    if int(raw.get("n_points") or n_points) != n_points:
        raise TurbulenceError("切片头 POINT_DATA 与 DIMENSIONS 不一致")
    fields = []
    for item in raw.get("fields") or []:
        try:
            fields.append(
                {
                    "name": str(item["name"]),
                    "ncomp": int(item.get("ncomp", 1)),
                    "nvals": int(item.get("nvals", n_points)),
                    "dtype": str(item["dtype"]).lower(),
                    "offset": int(item["offset"]),
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TurbulenceError(f"切片头字段信息无效: {exc}") from exc
    return {
        "dimensions": dims,
        "origin": origin,
        "spacing": spacing,
        "n_points": n_points,
        "fields": fields,
    }


def _check_subset_fields(tile: TurbulenceTile, file_size: int | None) -> None:
    for name, spec in tile.fields.items():
        if spec["dtype"] not in SUBSET_DTYPES or spec["ncomp"] != 1 or spec["nvals"] != tile.n_points:
            raise TurbulenceError(
                f"{tile.path.name} 的 {name} 不是 float/double 单分量场，无法按子集上传"
            )
        nbytes = tile.n_points * np.dtype(SUBSET_DTYPES[spec["dtype"]]).itemsize
        if spec["offset"] < 0 or (file_size is not None and spec["offset"] + nbytes > file_size):
            raise TurbulenceError(f"{tile.path.name} 的 {name} 数据超出文件范围，切片可能不完整")


def _axis_indices(coords: np.ndarray, origin: float, step: float, n: int, resampling: str) -> np.ndarray:
    """与 _sample_tile 相同的下标算法，得到某一轴上会被读到的切片下标。"""
    f = (coords - origin) / step
    if resampling in {"trilinear", "bilinear"}:
        f = f[(f >= 0) & (f <= n - 1)]
        lo = np.clip(np.floor(f).astype(np.int64), 0, max(n - 2, 0))
        idx = np.concatenate([lo, np.minimum(lo + 1, n - 1)])
    else:
        idx = np.rint(f).astype(np.int64)
        idx = idx[(idx >= 0) & (idx < n)]
    return np.unique(idx)


def plan_tile_subset(
    tile: TurbulenceTile,
    grid: TargetGrid,
    resampling: str,
) -> tuple[np.ndarray, np.ndarray, int, int]:
    """返回 (k_index, j_index, i_start, i_count)。气象网格是规则网格，三个轴互相独立。"""
    nx, ny, nz = tile.dimensions
    ii = _axis_indices(grid.xs(), tile.origin[0], tile.spacing[0], nx, resampling)
    jj = _axis_indices(grid.ys(), tile.origin[1], tile.spacing[1], ny, resampling)
    kk = _axis_indices(grid.zs(), tile.origin[2], tile.spacing[2], nz, resampling)
    if not (ii.size and jj.size and kk.size):
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty, 0, 0
    return kk, jj, int(ii[0]), int(ii[-1] - ii[0] + 1)


def subset_byte_ranges(
    tile: TurbulenceTile,
    k_index: np.ndarray,
    j_index: np.ndarray,
    i_start: int,
    i_count: int,
) -> list[list[int]]:
    """子集在原文件中的字节区间 [start, length]，按上传顺序（场 → k → j）排列，相邻区间已合并。"""
    if not (len(k_index) and len(j_index) and i_count):
        return []
    nx, ny, _ = tile.dimensions
    rows = (np.asarray(k_index, dtype=np.int64)[:, None] * ny + np.asarray(j_index, dtype=np.int64)[None, :]).ravel()
    starts_parts = []
    lengths_parts = []
    for spec in tile.fields.values():
        size = np.dtype(SUBSET_DTYPES[spec["dtype"]]).itemsize
        starts_parts.append(int(spec["offset"]) + (rows * nx + i_start) * size)
        lengths_parts.append(np.full(rows.size, i_count * size, dtype=np.int64))
    starts = np.concatenate(starts_parts)
    ends = starts + np.concatenate(lengths_parts)
    new_run = np.ones(starts.size, dtype=bool)
    new_run[1:] = starts[1:] != ends[:-1]
    first = np.flatnonzero(new_run)
    last = np.append(first[1:] - 1, starts.size - 1)
    return [[int(s), int(e - s)] for s, e in zip(starts[first], ends[last])]


def plan_turbulence_subsets(
    entries: list[dict[str, Any]],
    grid: TargetGrid,
    resampling: str = "nearest",
    field_filter: list[str] | None = None,
) -> dict[str, Any]:
    """entries: [{"name", "size", "header"}]。返回 manifest（融合时原样交回）和每个切片要读的字节区间。"""
    if not entries:
        raise TurbulenceError("没有风切变/湍流切片")
    resampling = (resampling or "nearest").lower()
    kept: list[tuple[dict[str, Any], dict[str, Any], TurbulenceTile]] = []
    for entry in entries:
        name = Path(str(entry.get("name") or "")).name
        if not name:
            raise TurbulenceError("切片缺少文件名")
        header = header_from_dict(entry.get("header") or {})
        tile = tile_from_header(Path(name), header, field_filter)
        if tile is None:
            logger.warning("切片没有可用标量场: %s", name)
            continue
        size = entry.get("size")
        _check_subset_fields(tile, int(size) if size is not None else None)
        kept.append((entry, header, tile))
    if not kept:
        raise TurbulenceError("没有可用的风切变/湍流切片")
    refine_tile_origins([tile for _, _, tile in kept])

    manifest_tiles = []
    transfers = []
    for _, header, tile in kept:
        k_index, j_index, i_start, i_count = plan_tile_subset(tile, grid, resampling)
        ranges = subset_byte_ranges(tile, k_index, j_index, i_start, i_count)
        manifest_tiles.append(
            {
                "name": tile.path.name,
                "header": {
                    "dimensions": list(header["dimensions"]),
                    "origin": list(header["origin"]),
                    "spacing": list(header["spacing"]),
                    "n_points": header["n_points"],
                    "fields": header["fields"],
                },
                "subset": {
                    "fields": list(tile.fields),
                    "k_index": [int(v) for v in k_index],
                    "j_index": [int(v) for v in j_index],
                    "i_start": i_start,
                    "i_count": i_count,
                },
            }
        )
        transfers.append(
            {
                "name": tile.path.name,
                "ranges": ranges,
                "nbytes": int(sum(length for _, length in ranges)),
            }
        )
    return {
        "manifest": {"version": 1, "resampling": resampling, "tiles": manifest_tiles},
        "transfers": transfers,
        "total_bytes": int(sum(item["nbytes"] for item in transfers)),
    }


def _index_array(values: Any, n: int, label: str) -> np.ndarray:
    arr = np.asarray(values if values is not None else [], dtype=np.int64).ravel()
    if arr.size and (arr[0] < 0 or arr[-1] >= n or np.any(np.diff(arr) <= 0)):
        raise TurbulenceError(f"子集 {label} 下标无效")
    return arr


def load_subset_tiles(folder: Path, field_filter: list[str] | None = None) -> list[TurbulenceTile]:
    """读取网页端上传的切片子集目录（SUBSET_MANIFEST + 每块一个 .bin）。"""
    folder = Path(folder)
    try:
        manifest = json.loads((folder / SUBSET_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TurbulenceError(f"无法读取切片子集清单: {exc}") from exc
    tiles: list[TurbulenceTile] = []
    for entry in manifest.get("tiles") or []:
        name = Path(str(entry.get("name") or "tile.vtk")).name
        header = header_from_dict(entry.get("header") or {})
        sub = entry.get("subset") or {}
        order = [str(n) for n in sub.get("fields") or []]
        tile = tile_from_header(folder / name, header, order or None)
        if tile is None or list(tile.fields) != order:
            raise TurbulenceError(f"切片子集 {name} 的字段与切片头不一致")
        _check_subset_fields(tile, None)
        nx, ny, nz = tile.dimensions
        k_index = _index_array(sub.get("k_index"), nz, "k")
        j_index = _index_array(sub.get("j_index"), ny, "j")
        i_start = int(sub.get("i_start") or 0)
        i_count = int(sub.get("i_count") or 0)
        if i_start < 0 or i_count < 0 or i_start + i_count > nx:
            raise TurbulenceError(f"切片子集 {name} 的 i 范围无效")

        data: dict[str, np.ndarray] = {}
        n_cells = k_index.size * j_index.size * i_count
        if n_cells:
            data_file = entry.get("data_file")
            if not data_file:
                raise TurbulenceError(f"缺少切片 {name} 的子集数据")
            raw = (folder / Path(str(data_file)).name).read_bytes()
            pos = 0
            for field_name in order:
                dtype = np.dtype(SUBSET_DTYPES[tile.fields[field_name]["dtype"]])
                if pos + n_cells * dtype.itemsize > len(raw):
                    raise TurbulenceError(f"切片 {name} 的子集数据不完整，请重新上传")
                arr = np.frombuffer(raw, dtype=dtype, count=n_cells, offset=pos)
                data[field_name] = arr.reshape(k_index.size, j_index.size, i_count).astype(np.float32)
                pos += n_cells * dtype.itemsize
            if pos != len(raw):
                raise TurbulenceError(f"切片 {name} 的子集数据大小不对（{len(raw)} 字节，应为 {pos}）")

        tile.subset = TileSubset(
            k_index=k_index,
            j_index=j_index,
            i_start=i_start,
            i_count=i_count,
            data=data,
        )
        tiles.append(tile)
    # 先用全部切片校正 ORIGIN（与 plan 时一致），再按 field_filter 筛选
    refine_tile_origins(tiles)
    if field_filter:
        for tile in tiles:
            tile.fields = {k: v for k, v in tile.fields.items() if k in field_filter}
        tiles = [tile for tile in tiles if tile.fields]
    if not tiles:
        raise TurbulenceError(f"切片子集里没有可用的场: {folder}")
    return tiles


class _SubsetVolume:
    """按原切片 (k, j, i) 下标读取 TileSubset，未覆盖的下标直接报错，避免静默得到错误结果。"""

    def __init__(self, tile: TurbulenceTile, field_name: str) -> None:
        sub = tile.subset
        assert sub is not None
        nx, ny, nz = tile.dimensions
        self._name = tile.path.name
        self._k = np.full(nz, -1, dtype=np.int64)
        self._k[sub.k_index] = np.arange(sub.k_index.size)
        self._j = np.full(ny, -1, dtype=np.int64)
        self._j[sub.j_index] = np.arange(sub.j_index.size)
        self._i_start = sub.i_start
        self._i_count = sub.i_count
        self._data = sub.data.get(field_name)

    def __getitem__(self, key: tuple[np.ndarray, np.ndarray, np.ndarray]) -> np.ndarray:
        kk, jj, ii = key
        kp = self._k[kk]
        jp = self._j[jj]
        ip = np.asarray(ii, dtype=np.int64) - self._i_start
        if (
            self._data is None
            or kp.min() < 0
            or jp.min() < 0
            or ip.min() < 0
            or ip.max() >= self._i_count
        ):
            raise TurbulenceError(
                f"切片 {self._name} 上传的子集没有覆盖气象网格需要的格点，请重新选择气象 VTK 后再融合"
            )
        return self._data[kp, jp, ip]


def _open_volume(tile: TurbulenceTile, field_name: str, spec: dict[str, Any]):
    if tile.subset is not None:
        return _SubsetVolume(tile, field_name)
    return memmap_field(tile.path, spec, tile.dimensions)


def inspect_turbulence(path: Path) -> dict:
    tiles = load_turbulence_tiles(path)
    bounds = [
        min(t.origin[0] for t in tiles),
        max(t.origin[0] + (t.dimensions[0] - 1) * t.spacing[0] for t in tiles),
        min(t.origin[1] for t in tiles),
        max(t.origin[1] + (t.dimensions[1] - 1) * t.spacing[1] for t in tiles),
        min(t.origin[2] for t in tiles),
        max(t.origin[2] + (t.dimensions[2] - 1) * t.spacing[2] for t in tiles),
    ]
    fields: list[str] = []
    for tile in tiles:
        for name in tile.fields:
            if name not in fields:
                fields.append(name)
    spacing = tiles[0].spacing
    return {
        "kind": "turbulence",
        "type": "风切变/湍流 VTK",
        "name": Path(path).name,
        "tiles": len(tiles),
        "fields": fields,
        "spacing": [float(v) for v in spacing],
        "bounds": [float(v) for v in bounds],
        "tile_names": [t.path.name for t in tiles],
        "dimensions": [int(v) for v in tiles[0].dimensions] if len(tiles) == 1 else None,
        "origin": [float(v) for v in tiles[0].origin] if len(tiles) == 1 else None,
    }


def _sample_tile(
    volume: np.ndarray,
    xx: np.ndarray,
    yy: np.ndarray,
    zz: np.ndarray,
    tile: TurbulenceTile,
    field_name: str,
    resampling: str,
) -> None:
    spec = tile.fields.get(field_name)
    if spec is None:
        return
    ox, oy, oz = tile.origin
    dx, dy, dz = tile.spacing
    nx, ny, nz = tile.dimensions
    xmin = ox
    xmax = ox + (nx - 1) * dx
    ymin = oy
    ymax = oy + (ny - 1) * dy
    zmin = oz
    zmax = oz + (nz - 1) * dz
    pad_x = abs(dx) * 0.51
    pad_y = abs(dy) * 0.51
    pad_z = abs(dz) * 0.51
    overlap = (
        float(xx.max()) >= xmin - pad_x
        and float(xx.min()) <= xmax + pad_x
        and float(yy.max()) >= ymin - pad_y
        and float(yy.min()) <= ymax + pad_y
        and float(zz.max()) >= zmin - pad_z
        and float(zz.min()) <= zmax + pad_z
    )
    if not overlap:
        return

    fi = (xx - ox) / dx
    fj = (yy - oy) / dy
    fk = (zz - oz) / dz
    vacant = ~np.isfinite(volume)

    if resampling in {"trilinear", "bilinear"}:
        inside = (
            (fi >= 0)
            & (fi <= nx - 1)
            & (fj >= 0)
            & (fj <= ny - 1)
            & (fk >= 0)
            & (fk <= nz - 1)
            & vacant
        )
        if not np.any(inside):
            return
        mm = _open_volume(tile, field_name, spec)
        try:
            i0 = np.floor(fi).astype(np.int64)
            j0 = np.floor(fj).astype(np.int64)
            k0 = np.floor(fk).astype(np.int64)
            i0 = np.clip(i0, 0, nx - 2)
            j0 = np.clip(j0, 0, ny - 2)
            k0 = np.clip(k0, 0, nz - 2)
            wx = (fi - i0).astype(np.float32)
            wy = (fj - j0).astype(np.float32)
            wz = (fk - k0).astype(np.float32)
            idx = np.nonzero(inside)
            ii, jj, kk = i0[idx], j0[idx], k0[idx]
            c000 = np.asarray(mm[kk, jj, ii], dtype=np.float32)
            c100 = np.asarray(mm[kk, jj, ii + 1], dtype=np.float32)
            c010 = np.asarray(mm[kk, jj + 1, ii], dtype=np.float32)
            c110 = np.asarray(mm[kk, jj + 1, ii + 1], dtype=np.float32)
            c001 = np.asarray(mm[kk + 1, jj, ii], dtype=np.float32)
            c101 = np.asarray(mm[kk + 1, jj, ii + 1], dtype=np.float32)
            c011 = np.asarray(mm[kk + 1, jj + 1, ii], dtype=np.float32)
            c111 = np.asarray(mm[kk + 1, jj + 1, ii + 1], dtype=np.float32)
            tx, ty, tz = wx[idx], wy[idx], wz[idx]
            c00 = c000 * (1 - tx) + c100 * tx
            c01 = c001 * (1 - tx) + c101 * tx
            c10 = c010 * (1 - tx) + c110 * tx
            c11 = c011 * (1 - tx) + c111 * tx
            c0 = c00 * (1 - ty) + c10 * ty
            c1 = c01 * (1 - ty) + c11 * ty
            volume[idx] = c0 * (1 - tz) + c1 * tz
        finally:
            del mm
        return

    i = np.rint(fi).astype(np.int64)
    j = np.rint(fj).astype(np.int64)
    k = np.rint(fk).astype(np.int64)
    inside = (i >= 0) & (i < nx) & (j >= 0) & (j < ny) & (k >= 0) & (k < nz) & vacant
    if not np.any(inside):
        return
    mm = _open_volume(tile, field_name, spec)
    try:
        idx = np.nonzero(inside)
        volume[idx] = np.asarray(mm[k[idx], j[idx], i[idx]], dtype=np.float32)
    finally:
        del mm


def sample_field_to_grid(
    tiles: list[TurbulenceTile],
    field_name: str,
    grid: TargetGrid,
    resampling: str,
) -> np.ndarray:
    xs, ys, zs = grid.xs(), grid.ys(), grid.zs()
    xx, yy, zz = np.meshgrid(xs, ys, zs, indexing="ij")
    volume = np.full(xx.shape, np.nan, dtype=np.float32)
    for tile in tiles:
        _sample_tile(volume, xx, yy, zz, tile, field_name, resampling)
    return volume


def overlay_turbulence(
    mesh: pv.DataSet,
    vtk: VtkSource,
    sources: list[TurbulenceSource],
) -> pv.DataSet:
    if not sources:
        return mesh
    used = set(mesh.point_data.keys()) | set(mesh.cell_data.keys())
    if isinstance(mesh, pv.ImageData):
        from stfusion.resample import grid_from_image

        grid = grid_from_image(mesh, vtk.crs)
        n = grid.nx * grid.ny * grid.nz
        for src in sources:
            for field_name in src.fields:
                volume = sample_field_to_grid(src.tiles, field_name, grid, src.resampling)
                finite = np.isfinite(volume)
                logger.info(
                    "湍流 %s：切片 %s，有效点 %s/%s",
                    field_name,
                    len(src.tiles),
                    int(finite.sum()),
                    finite.size,
                )
                name = field_name if field_name not in used else f"{src.name}_{field_name}"
                mesh.point_data[name] = np.ascontiguousarray(volume.reshape(n, order="F"))
                used.add(name)
        return mesh

    pts = np.asarray(mesh.points, dtype=np.float64)
    xx = pts[:, 0]
    yy = pts[:, 1]
    zz = pts[:, 2]
    for src in sources:
        for field_name in src.fields:
            values = np.full(pts.shape[0], np.nan, dtype=np.float32)
            for tile in src.tiles:
                _sample_tile(values, xx, yy, zz, tile, field_name, src.resampling)
            name = field_name if field_name not in used else f"{src.name}_{field_name}"
            mesh.point_data[name] = values
            used.add(name)
    return mesh
