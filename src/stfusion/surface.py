"""建筑和地形共用的 ZIP 读取及二维字段写入。"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import numpy as np
import pyvista as pv

from stfusion.models import TargetGrid
from stfusion.resample import grid_from_image


class SurfaceError(ValueError):
    pass


class TileArchive:
    def __init__(self, path: Path, manifest: str):
        self.path = path
        self.manifest = manifest

    def __enter__(self):
        self.zip = ZipFile(self.path)
        try:
            self.names = set()
            for entry in self.zip.infolist():
                name = entry.filename
                parts = PurePosixPath(name).parts
                if "\\" in name or ":" in name or name.startswith("/") or ".." in parts:
                    raise SurfaceError(f"ZIP 中含无效路径：{name}")
                if entry.is_dir():
                    continue
                if name in self.names or entry.flag_bits & 1:
                    raise SurfaceError(f"ZIP 中含重复或加密文件：{name}")
                self.names.add(name)
            roots = [n for n in self.names if PurePosixPath(n).name == self.manifest]
            if len(roots) != 1:
                raise SurfaceError(f"{self.path.name} 需要唯一的 {self.manifest}")
            self.root = str(PurePosixPath(roots[0]).parent)
            self.root = "" if self.root == "." else self.root + "/"
            self.metadata = self.read_json(self.manifest)
            return self
        except Exception:
            self.zip.close()
            raise

    def __exit__(self, *args):
        self.zip.close()

    def read(self, name: str) -> bytes:
        parts = PurePosixPath(name).parts
        if not parts or ".." in parts or "\\" in name or ":" in name or name.startswith("/"):
            raise SurfaceError(f"无效的瓦片引用：{name}")
        member = self.root + name
        if member not in self.names:
            raise SurfaceError(f"{self.path.name} 缺少文件：{name}")
        return self.zip.read(member)

    def read_json(self, name: str) -> dict:
        data = json.loads(self.read(name).decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise SurfaceError(f"{name} 需要是 JSON 对象")
        return data


def surface_grid(mesh: pv.DataSet, crs) -> TargetGrid:
    if not isinstance(mesh, pv.ImageData):
        raise SurfaceError("地形和建筑融合需要规则网格 ImageData")
    if not np.allclose(mesh.direction_matrix, np.eye(3), rtol=0, atol=1e-12):
        raise SurfaceError("地形和建筑融合需要轴对齐的气象网格")
    if not np.isfinite(mesh.spacing).all() or min(mesh.spacing) <= 0:
        raise SurfaceError("气象网格间距需要是正数")
    return grid_from_image(mesh, crs)


def add_surface(mesh: pv.DataSet, grid: TargetGrid, name: str, xy: np.ndarray) -> None:
    if name in mesh.cell_data:
        raise SurfaceError(f"{name} 已存在于单元字段中，不能写入同名点字段")
    if xy.shape != (grid.ny, grid.nx):
        raise SurfaceError(f"{name} 二维采样尺寸与气象网格不一致")
    values = np.empty(grid.nx * grid.ny * grid.nz, dtype=np.float32)
    values.reshape(grid.nz, -1)[:] = xy.ravel(order="C")
    mesh.point_data[name] = values
