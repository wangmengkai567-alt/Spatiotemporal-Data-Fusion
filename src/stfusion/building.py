"""读取 B3DM 建筑几何，将最大 Height 栅格化到气象 XY 网格。"""

from __future__ import annotations

import itertools
import json
import struct

import numpy as np
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from stfusion.models import BuildingSource, TargetGrid
from stfusion.surface import SurfaceError, TileArchive


_COMPONENTS = {5120: "i1", 5121: "u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}
_WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
_BATCH_TYPES = {"BYTE": "i1", "UNSIGNED_BYTE": "u1", "SHORT": "<i2", "UNSIGNED_SHORT": "<u2",
                "INT": "<i4", "UNSIGNED_INT": "<u4", "FLOAT": "<f4", "DOUBLE": "<f8"}
_Y_UP = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], dtype=float)


def _matrix(node: dict) -> np.ndarray:
    if "matrix" in node:
        matrix = np.asarray(node["matrix"], dtype=np.float64).reshape(4, 4, order="F")
    else:
        x, y, z, w = node.get("rotation", [0, 0, 0, 1])
        matrix = np.eye(4)
        matrix[:3, :3] = [
            [1 - 2*(y*y + z*z), 2*(x*y - z*w), 2*(x*z + y*w)],
            [2*(x*y + z*w), 1 - 2*(x*x + z*z), 2*(y*z - x*w)],
            [2*(x*z - y*w), 2*(y*z + x*w), 1 - 2*(x*x + y*y)],
        ]
        matrix[:3, :3] *= np.asarray(node.get("scale", [1, 1, 1]))
        matrix[:3, 3] = node.get("translation", [0, 0, 0])
    if not np.isfinite(matrix).all():
        raise SurfaceError("建筑变换矩阵包含无效值")
    return matrix


def _transform(points: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def _intersects(tile: dict, matrix: np.ndarray, transformer: Transformer, grid: TargetGrid) -> bool:
    volume = tile["boundingVolume"]
    if "region" in volume:
        west, south, east, north = np.rad2deg(volume["region"][:4])
        xmin, ymin, xmax, ymax = transform_bounds("EPSG:4326", grid.crs, west, south, east, north, densify_pts=21)
    else:
        if "box" in volume:
            values = np.asarray(volume["box"], dtype=float)
            center, axes = values[:3], values[3:].reshape(3, 3)
        elif "sphere" in volume:
            values = np.asarray(volume["sphere"], dtype=float)
            center, axes = values[:3], np.eye(3) * values[3]
        else:
            raise SurfaceError("不支持的建筑包围体")
        # 加密包围体表面，保守筛选投影后的 XY 范围。
        corners = np.array(list(itertools.product((-1, 0, 1), repeat=3))) @ axes + center
        global_points = _transform(corners, matrix)
        xs, ys, _ = transformer.transform(*global_points.T)
        xmin, xmax, ymin, ymax = np.min(xs), np.max(xs), np.min(ys), np.max(ys)
    bounds = grid.bounds
    dx, dy = grid.spacing[:2]
    return xmin <= bounds.xmax + dx and xmax >= bounds.xmin - dx and ymin <= bounds.ymax + dy and ymax >= bounds.ymin - dy


def _accessor(gltf: dict, binary: memoryview, index: int) -> np.ndarray:
    accessor = gltf["accessors"][index]
    if "sparse" in accessor or accessor.get("normalized", False):
        raise SurfaceError("建筑暂不支持 sparse 或 normalized 属性")
    view = gltf["bufferViews"][accessor["bufferView"]]
    if view["buffer"] != 0:
        raise SurfaceError("建筑只支持内嵌 GLB 缓冲区")
    dtype = np.dtype(_COMPONENTS[accessor["componentType"]])
    width = _WIDTHS[accessor["type"]]
    count = int(accessor["count"])
    stride = int(view.get("byteStride", dtype.itemsize * width))
    relative = int(accessor.get("byteOffset", 0))
    required = relative + max(count - 1, 0) * stride + dtype.itemsize * width
    offset = int(view.get("byteOffset", 0))
    if count <= 0 or stride < dtype.itemsize * width or required > view["byteLength"] or offset + required > len(binary):
        raise SurfaceError("建筑 accessor 长度或步长错误")
    result = np.ndarray((count, width), dtype=dtype, buffer=binary, offset=offset + relative,
                        strides=(stride, dtype.itemsize))
    return result[:, 0] if width == 1 else result


def _read_b3dm(data: bytes):
    if len(data) < 28:
        raise SurfaceError("B3DM 文件头不完整")
    magic, version, length, ft_json, ft_bin, bt_json, bt_bin = struct.unpack_from("<4s6I", data)
    if magic != b"b3dm" or version != 1 or length != len(data):
        raise SurfaceError("B3DM 格式或文件长度错误")
    offset = 28
    feature = json.loads(data[offset:offset + ft_json])
    offset += ft_json + ft_bin
    batch = json.loads(data[offset:offset + bt_json])
    offset += bt_json
    batch_binary = memoryview(data)[offset:offset + bt_bin]
    offset += bt_bin
    count = int(feature["BATCH_LENGTH"])
    height_property = batch["Height"]
    if isinstance(height_property, list):
        heights = np.asarray(height_property, dtype=np.float64)
    else:
        if height_property["type"] != "SCALAR":
            raise SurfaceError("建筑 Height 必须是标量")
        dtype = np.dtype(_BATCH_TYPES[height_property["componentType"]])
        heights = np.frombuffer(batch_binary, dtype=dtype, count=count, offset=height_property["byteOffset"])
    if heights.shape != (count,) or not np.isfinite(heights).all() or np.any(heights < 0):
        raise SurfaceError("建筑 Height 缺失、数量错误或含无效值")
    glb = memoryview(data)[offset:]
    if len(glb) < 20:
        raise SurfaceError("缺少内嵌 GLB")
    glb_magic, glb_version, glb_length = struct.unpack_from("<4sII", glb)
    if glb_magic != b"glTF" or glb_version != 2 or glb_length > len(glb):
        raise SurfaceError("建筑需要 glTF 2.0 的 GLB")
    chunks = {}
    offset = 12
    while offset < glb_length:
        size, kind = struct.unpack_from("<II", glb, offset)
        offset += 8
        if offset + size > glb_length or kind in chunks:
            raise SurfaceError("GLB 块长度或数量错误")
        chunks[kind] = glb[offset:offset + size]
        offset += size
    gltf = json.loads(bytes(chunks[0x4E4F534A]))
    if gltf.get("extensionsRequired"):
        raise SurfaceError("建筑包含暂不支持的 glTF 必需扩展")
    buffers = gltf["buffers"]
    if len(buffers) != 1 or "uri" in buffers[0]:
        raise SurfaceError("建筑只支持一个内嵌 GLB 缓冲区")
    binary = chunks[0x004E4942]
    if buffers[0]["byteLength"] > len(binary):
        raise SurfaceError("建筑 GLB 缓冲区长度错误")
    rtc = np.eye(4)
    if "RTC_CENTER" in feature:
        if not isinstance(feature["RTC_CENTER"], list):
            raise SurfaceError("建筑 RTC_CENTER 需要三个数值")
        rtc[:3, 3] = feature["RTC_CENTER"]
    return gltf, binary, heights, rtc


def _primitives(gltf: dict, binary: memoryview, heights: np.ndarray, base: np.ndarray):
    nodes = gltf["nodes"]

    def visit(index, parent, ancestors):
        if index in ancestors:
            raise SurfaceError("glTF 节点存在循环引用")
        node = nodes[index]
        matrix = parent @ _matrix(node)
        if "skin" in node:
            raise SurfaceError("建筑不支持蒙皮节点")
        if "mesh" in node:
            for primitive in gltf["meshes"][node["mesh"]]["primitives"]:
                if primitive.get("mode", 4) != 4 or primitive.get("extensions") or primitive.get("targets"):
                    raise SurfaceError("建筑只支持未压缩的静态三角形几何")
                attributes = primitive["attributes"]
                points = _accessor(gltf, binary, attributes["POSITION"])
                batches = _accessor(gltf, binary, attributes["_BATCHID"])
                if points.ndim != 2 or points.shape[1] != 3 or batches.shape != (len(points),):
                    raise SurfaceError("建筑顶点或 _BATCHID 维度错误")
                if (not np.isfinite(points).all() or not np.isfinite(batches).all()
                        or np.any(batches != np.floor(batches)) or np.any((batches < 0) | (batches >= len(heights)))):
                    raise SurfaceError("建筑坐标或 _BATCHID 无效")
                if "indices" in primitive:
                    indices = _accessor(gltf, binary, primitive["indices"])
                else:
                    indices = np.arange(len(points))
                if indices.ndim != 1 or indices.dtype.kind not in "ui" or len(indices) % 3 or np.any(indices >= len(points)):
                    raise SurfaceError("建筑三角形索引无效")
                triangles = indices.reshape(-1, 3)
                ids = batches[triangles]
                if np.any(ids != ids[:, :1]):
                    raise SurfaceError("建筑三角形的顶点属于不同建筑")
                yield _transform(points, base @ matrix), triangles, heights[ids[:, 0].astype(np.int64)]
        for child in node.get("children", []):
            yield from visit(child, matrix, ancestors | {index})

    for root in gltf["scenes"][gltf.get("scene", 0)]["nodes"]:
        yield from visit(root, np.eye(4), set())


def _rasterize(points: np.ndarray, triangles: np.ndarray, heights: np.ndarray,
               result: np.ndarray, grid: TargetGrid) -> None:
    bounds = grid.bounds
    dx, dy = grid.spacing[:2]
    transform = from_origin(bounds.xmin - dx / 2, bounds.ymax + dy / 2, dx, dy)
    # 每批最多展开 8192 个三角形，避免构造全部几何的 Python 对象。
    for start in range(0, len(triangles), 8192):
        projected = points[triangles[start:start + 8192], :2]
        values = heights[start:start + 8192]
        low, high = projected.min(axis=1), projected.max(axis=1)
        a, b = projected[:, 1] - projected[:, 0], projected[:, 2] - projected[:, 0]
        area = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
        mask = ((high[:, 0] >= bounds.xmin - dx/2) & (low[:, 0] <= bounds.xmax + dx/2)
                & (high[:, 1] >= bounds.ymin - dy/2) & (low[:, 1] <= bounds.ymax + dy/2)
                & (abs(area) > 1e-8))
        selected = np.flatnonzero(mask)
        if not selected.size:
            continue
        selected = selected[np.argsort(values[selected], kind="stable")]
        polygons = projected[selected]
        rings = np.concatenate((polygons, polygons[:, :1]), axis=1)
        sorted_heights = values[selected]
        starts = np.r_[0, np.flatnonzero(np.diff(sorted_heights)) + 1]
        ends = np.r_[starts[1:], len(selected)]
        # 同高度三角形作为一个 MultiPolygon 交给 GDAL，避免逐面构造和解析 feature。
        # 仍使用全部原始三角形，不改变投影范围、孔洞或最大高度规则。
        shapes = [
            ({"type": "MultiPolygon", "coordinates": rings[begin:end, None].tolist()},
             float(sorted_heights[begin]))
            for begin, end in zip(starts, ends)
        ]
        sampled = rasterize(shapes, out_shape=result.shape, transform=transform,
                            fill=np.nan, dtype="float32", all_touched=True, skip_invalid=False)
        np.fmax(result, sampled[::-1], out=result)


def sample_buildings(source: BuildingSource, grid: TargetGrid) -> np.ndarray:
    result = np.full((grid.ny, grid.nx), np.nan, dtype=np.float32)
    transformer = Transformer.from_crs("EPSG:4978", grid.crs, always_xy=True)
    with TileArchive(source.path, "tileset.json") as archive:
        if archive.metadata.get("asset", {}).get("version") != "1.0":
            raise SurfaceError("建筑需要包含 B3DM 的 3D Tiles 1.0")

        def visit(tile, parent):
            matrix = parent @ np.asarray(tile.get("transform", np.eye(4).ravel(order="F")), dtype=float).reshape(4, 4, order="F")
            if not _intersects(tile, matrix, transformer, grid):
                return
            if "content" in tile:
                if tile.get("children"):
                    raise SurfaceError("建筑暂不支持同时包含内容和子瓦片的细化结构")
                name = tile["content"]["uri"]
                if not name.lower().endswith(".b3dm"):
                    raise SurfaceError(f"建筑不支持的瓦片类型：{name}")
                try:
                    gltf, binary, heights, rtc = _read_b3dm(archive.read(name))
                    for points, triangles, values in _primitives(gltf, binary, heights, matrix @ rtc @ _Y_UP):
                        x, y, z = transformer.transform(*points.T)
                        projected = np.column_stack((x, y, z))
                        if not np.isfinite(projected).all():
                            raise SurfaceError("建筑转换后坐标无效")
                        _rasterize(projected, triangles, values, result, grid)
                    del gltf, binary, heights
                except Exception as exc:
                    raise SurfaceError(f"建筑瓦片 {name}：{exc}") from exc
            for child in tile.get("children", []):
                visit(child, matrix)

        visit(archive.metadata["root"], np.eye(4))
    # tileset 包围盒不证明数据完整，未覆盖建筑的位置保持 NaN。
    return result
