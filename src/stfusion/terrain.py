"""从 quantized-mesh ZIP 按需采样地形高程。"""

from __future__ import annotations

import gzip
import json
import struct

import numpy as np
import pyvista as pv
from pyproj import Transformer

from stfusion.models import TargetGrid, TerrainSource
from stfusion.surface import SurfaceError, TileArchive


def _decode_tile(data: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if len(data) < 92:
        raise SurfaceError("地形瓦片头不完整")
    minimum, maximum = struct.unpack_from("<2f", data, 24)
    count = struct.unpack_from("<I", data, 88)[0]
    offset = 92 + count * 6
    if count < 3 or offset > len(data):
        raise SurfaceError("地形顶点数量或长度错误")
    encoded = np.frombuffer(data, dtype="<u2", count=count * 3, offset=92)
    encoded = encoded.reshape(3, count).astype(np.int64)
    decoded = np.cumsum((encoded >> 1) ^ -(encoded & 1), axis=1)
    if np.any((decoded < 0) | (decoded > 32767)):
        raise SurfaceError("地形顶点量化值越界")
    dtype = np.dtype("<u4" if count > 65536 else "<u2")
    offset = (offset + dtype.itemsize - 1) // dtype.itemsize * dtype.itemsize
    triangles = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    codes = np.frombuffer(data, dtype=dtype, count=triangles * 3, offset=offset).astype(np.int64)
    zero = codes == 0
    # 按格式的无符号索引宽度解码，CesiumLab 瓦片可能含回绕编码值。
    indices = (np.cumsum(zero) - zero - codes) & np.iinfo(dtype).max
    if triangles == 0 or np.any((indices < 0) | (indices >= count)):
        raise SurfaceError("地形三角形索引无效")
    offset += codes.size * dtype.itemsize
    for _ in range(4):
        edge_count = struct.unpack_from("<I", data, offset)[0]
        offset += 4 + edge_count * dtype.itemsize
        if offset > len(data):
            raise SurfaceError("地形边缘索引长度错误")
    metadata = {}
    while offset < len(data):
        extension, length = struct.unpack_from("<BI", data, offset)
        offset += 5
        end = offset + length
        if end > len(data):
            raise SurfaceError("地形扩展长度错误")
        if extension == 4:
            json_length = struct.unpack_from("<I", data, offset)[0]
            if json_length + 4 > length:
                raise SurfaceError("地形可用性元数据长度错误")
            metadata = json.loads(data[offset + 4:offset + 4 + json_length])
        offset = end
    points = np.column_stack((decoded[0] / 32767.0, decoded[1] / 32767.0, np.zeros(count)))
    heights = minimum + (maximum - minimum) * decoded[2] / 32767.0
    if not np.isfinite(heights).all():
        raise SurfaceError("地形包含无效高程")
    return points, indices.reshape(-1, 3), heights, metadata


def sample_terrain(source: TerrainSource, grid: TargetGrid) -> np.ndarray:
    result = np.full((grid.ny, grid.nx), np.nan, dtype=np.float32)
    xx, yy = np.meshgrid(grid.xs(), grid.ys())
    lon, lat = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True).transform(xx.ravel(), yy.ravel())
    with TileArchive(source.path, "layer.json") as archive:
        layer = archive.metadata
        if (layer.get("format") != "quantized-mesh-1.0"
                or layer.get("projection", "EPSG:4326") != "EPSG:4326"
                or layer.get("scheme", "tms") != "tms"):
            raise SurfaceError("地形需要 EPSG:4326、TMS 编号的 quantized-mesh-1.0")
        level = int(layer["maxzoom"])
        if not 0 <= level <= 30:
            raise SurfaceError("地形 maxzoom 无效")
        templates = layer.get("tiles")
        if not isinstance(templates, list) or len(templates) != 1:
            raise SurfaceError("地形需要唯一的瓦片路径模板")
        template = templates[0]
        width = 180.0 / 2**level
        ix = np.clip(np.floor((lon + 180.0) / width), 0, 2**(level + 1) - 1).astype(np.int64)
        iy = np.clip(np.floor((lat + 90.0) / width), 0, 2**level - 1).astype(np.int64)
        valid = np.isfinite(lon) & np.isfinite(lat) & (abs(lon) <= 180) & (abs(lat) <= 90)
        bounds = layer.get("valid_bounds")
        if bounds is not None:
            valid &= (lon >= bounds[0]) & (lon <= bounds[2]) & (lat >= bounds[1]) & (lat <= bounds[3])
        available = layer.get("available") if "metadataAvailability" not in layer else None
        metadata_cache = {}

        def tile_name(z, x, y):
            return template.format(z=z, x=x, y=y, version=layer.get("version", "1.0.0")).split("?", 1)[0]

        def declared_available(x, y):
            if available is not None:
                ranges = available[level] if level < len(available) else []
            elif "metadataAvailability" in layer:
                interval = int(layer["metadataAvailability"])
                if interval <= 0:
                    raise SurfaceError("地形 metadataAvailability 无效")
                ranges = None
                for parent_level in range(0, level, interval):
                    shift = level - parent_level
                    parent_name = tile_name(parent_level, x >> shift, y >> shift)
                    if parent_name not in metadata_cache:
                        if archive.root + parent_name not in archive.names:
                            continue
                        *_, metadata_cache[parent_name] = _decode_tile(archive.read(parent_name))
                    descendants = metadata_cache[parent_name].get("available", [])
                    relative_level = level - parent_level - 1
                    if relative_level < len(descendants):
                        ranges = descendants[relative_level]
                        break
            else:
                return False
            return ranges is not None and any(
                r["startX"] <= x <= r["endX"] and r["startY"] <= y <= r["endY"] for r in ranges
            )

        keys = ix + iy * 2**(level + 1)
        order = np.argsort(keys[valid], kind="stable")
        selected = np.flatnonzero(valid)[order]
        groups = np.split(selected, np.flatnonzero(np.diff(keys[selected])) + 1)
        for chosen in groups:
            if not chosen.size:
                continue
            x, y = int(ix[chosen[0]]), int(iy[chosen[0]])
            name = tile_name(level, x, y)
            declared = declared_available(x, y)
            if archive.root + name not in archive.names:
                if declared:
                    raise SurfaceError(f"地形元数据声明的瓦片缺失：{name}")
                continue
            try:
                points, triangles, heights, _ = _decode_tile(archive.read(name))
                faces = np.column_stack((np.full(len(triangles), 3), triangles)).ravel()
                surface = pv.PolyData(points, faces)
                surface.point_data["height"] = heights
                queries = np.column_stack(((lon[chosen] + 180) / width - x,
                                           (lat[chosen] + 90) / width - y, np.zeros(chosen.size)))
                sampled = pv.PolyData(queries).sample(
                    surface, tolerance=1e-9, pass_cell_data=False, pass_point_data=False,
                    mark_blank=False, snap_to_closest_point=False,
                )
                mask = np.asarray(sampled["vtkValidPointMask"], dtype=bool)
                result.ravel()[chosen[mask]] = sampled["height"][mask]
            except Exception as exc:
                raise SurfaceError(f"地形瓦片 {name}：{exc}") from exc
    return result
