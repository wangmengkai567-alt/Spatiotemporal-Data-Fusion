"""网页服务：提供页面、上传接口"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "output"
VTK_SUFFIXES = {".vtk", ".vti", ".vtp", ".vts", ".vtu", ".vtr"}
TIF_SUFFIXES = {".tif", ".tiff", ".geotiff"}
WRF_SUFFIXES = {".nc", ".nc4", ".cdf"}


def _safe_name(upload: UploadFile, fallback: str) -> str:
    return Path(upload.filename or fallback).name


def _looks_like_wrf_upload(filename: str | None) -> bool:
    name = (filename or "").lower()
    suffix = Path(filename or "").suffix.lower()
    if suffix in WRF_SUFFIXES:
        return True
    if name.startswith("wrfout") or "wrfout" in Path(filename or "").name.lower():
        return True
    return not suffix


def _allowed_inspect(filename: str | None) -> bool:
    suffix = Path(filename or "").suffix.lower()
    if suffix in VTK_SUFFIXES | TIF_SUFFIXES | WRF_SUFFIXES:
        return True
    return _looks_like_wrf_upload(filename)


def _format_size(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes} B"
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.2f} KB"
    return f"{nbytes / (1024 * 1024):.2f} MB"


def _format_mtime(path: Path) -> str:
    stamp = datetime.fromtimestamp(path.stat().st_mtime)
    return f"{stamp.year}/{stamp.month}/{stamp.day} {stamp.strftime('%H:%M:%S')}"


def _safe_output_stem(name: str) -> str:
    stem = Path(name).stem.replace(" ", "_")
    cleaned = "".join(ch for ch in stem if ch.isalnum() or ch in {"_", "-", "."})
    return (cleaned or "fused")[:200]


def _output_vtk_path(name: str) -> Path:
    safe = Path(name).name
    if Path(safe).suffix.lower() not in VTK_SUFFIXES:
        raise HTTPException(status_code=400, detail="只能操作 VTK 文件")
    path = (OUTPUT_DIR / safe).resolve()
    if path.parent != OUTPUT_DIR.resolve():
        raise HTTPException(status_code=400, detail="无效的文件名")
    return path


def _item_from_vtk(path: Path) -> dict:
    meta_path = path.with_suffix(".json")
    extra: dict = {}
    if meta_path.is_file():
        try:
            extra = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            extra = {}
    stat = path.stat()
    return {
        "name": path.name,
        "size": int(stat.st_size),
        "size_text": _format_size(int(stat.st_size)),
        "mtime": _format_mtime(path),
        "fields": extra.get("fields") or [],
        "source": extra.get("source") or "",
        "vtk_crs": extra.get("vtk_crs") or "",
        "turbulence_tiles": extra.get("turbulence_tiles") or 0,
    }


def _list_outputs() -> list[dict]:
    if not OUTPUT_DIR.is_dir():
        return []
    paths = [
        path
        for path in OUTPUT_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in VTK_SUFFIXES
    ]
    paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [_item_from_vtk(path) for path in paths]


def fusion_status() -> dict:
    missing: list[str] = []
    for name in ("numpy", "pyvista", "rasterio", "pyproj"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    return {"ready": not missing, "missing": missing}


def create_app() -> FastAPI:
    app = FastAPI(title="时空数据融合", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://spatiotemporal.cn"],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict:
        return fusion_status()

    @app.post("/api/inspect")
    async def inspect(file: UploadFile = File(...)) -> dict:
        _require_fusion()
        suffix = Path(file.filename or "").suffix.lower()
        if not _allowed_inspect(file.filename):
            raise HTTPException(status_code=400, detail="请上传 VTK、GeoTIFF、湍流 VTK 或 WRF wrfout 文件")
        from stfusion.io import inspect_info

        with tempfile.TemporaryDirectory(prefix="stfusion-inspect-") as tmp:
            dest = Path(tmp) / _safe_name(file, f"upload{suffix or ''}")
            await _save_upload(file, dest)
            try:
                return inspect_info(dest)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/turbulence/plan")
    def turbulence_plan(payload: dict = Body(...)) -> dict:
        """根据气象网格和切片头，返回每个切片需要上传的字节区间。"""
        _require_fusion()
        from pyproj import CRS

        from stfusion.models import TargetGrid
        from stfusion.turbulence import TurbulenceError, plan_turbulence_subsets

        grid_raw = payload.get("grid") or {}
        try:
            grid = TargetGrid(
                crs=CRS.from_epsg(32649),
                origin=tuple(float(v) for v in grid_raw["origin"]),
                spacing=tuple(float(v) for v in grid_raw["spacing"]),
                dimensions=tuple(int(v) for v in grid_raw["dimensions"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="气象 VTK 需要是规则网格（ImageData）") from exc
        if len(grid.origin) != 3 or len(grid.spacing) != 3 or len(grid.dimensions) != 3:
            raise HTTPException(status_code=400, detail="气象网格 origin/spacing/dimensions 需要 3 个分量")
        try:
            return plan_turbulence_subsets(
                list(payload.get("tiles") or []),
                grid,
                str(payload.get("resampling") or "nearest"),
            )
        except TurbulenceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/fuse")
    async def fuse(
        vtk: UploadFile = File(...),
        geotiff: list[UploadFile] = File(default=[]),
        geotiff_field: list[str] = Form(default=[]),
        wrf: UploadFile | None = File(default=None),
        wrf_field: str = Form(""),
        wrf_var: str = Form("QVAPOR"),
        turbulence: list[UploadFile] = File(default=[]),
        turbulence_subset: list[UploadFile] = File(default=[]),
        turbulence_manifest: str = Form(""),
        vtk_crs: str = Form("EPSG:32649"),
        target_crs: str = Form("EPSG:32649"),
        strategy: str = Form("coarsest"),
        resampling: str = Form("bilinear"),
    ) -> JSONResponse:
        _require_fusion()
        geotiff = [item for item in geotiff if item is not None and item.filename]
        turbulence = [item for item in turbulence if item is not None and item.filename]
        turbulence_subset = [item for item in turbulence_subset if item is not None and item.filename]
        manifest = None
        if turbulence_manifest.strip():
            try:
                manifest = json.loads(turbulence_manifest)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="turbulence_manifest 不是有效 JSON") from exc
            if not isinstance(manifest, dict) or not manifest.get("tiles"):
                raise HTTPException(status_code=400, detail="turbulence_manifest 缺少切片")
            if turbulence:
                raise HTTPException(status_code=400, detail="完整切片和切片子集不能同时上传")
        wrf_ok = wrf is not None and bool(wrf.filename)
        if not geotiff and not wrf_ok and not turbulence and manifest is None:
            raise HTTPException(
                status_code=400,
                detail="请至少提供人口 GeoTIFF、wrfout 湿度或风切变/湍流数据",
            )

        vtk_suffix = Path(vtk.filename or ".vtk").suffix.lower()
        if vtk_suffix not in VTK_SUFFIXES:
            raise HTTPException(status_code=400, detail="气象数据需要是 VTK 文件")

        tmp = tempfile.mkdtemp(prefix="stfusion-fuse-")
        tmp_path = Path(tmp)
        try:
            vtk_path = tmp_path / _safe_name(vtk, "weather.vtk")
            await _save_upload(vtk, vtk_path)

            pairs: list[tuple[Path, str]] = []
            for i, item in enumerate(geotiff):
                suffix = Path(item.filename or ".tif").suffix.lower()
                if suffix not in TIF_SUFFIXES:
                    raise HTTPException(
                        status_code=400, detail=f"{item.filename} 不是 GeoTIFF"
                    )
                dest = tmp_path / _safe_name(item, f"layer_{i}.tif")
                await _save_upload(item, dest)
                field = (
                    geotiff_field[i].strip()
                    if i < len(geotiff_field) and geotiff_field[i].strip()
                    else ("population" if i == 0 else dest.stem)
                )
                pairs.append((dest, field))

            wrf_path = None
            if wrf_ok:
                if wrf is None:
                    raise HTTPException(status_code=400, detail="wrfout 文件无效")
                wrf_path = tmp_path / _safe_name(wrf, "wrfout")
                await _save_upload(wrf, wrf_path)

            turb_path = None
            turb_tiles = 0
            if turbulence:
                turb_path = tmp_path / "turbulence"
                turb_path.mkdir(parents=True, exist_ok=True)
                for i, item in enumerate(turbulence):
                    suffix = Path(item.filename or ".vtk").suffix.lower()
                    if suffix not in VTK_SUFFIXES:
                        raise HTTPException(
                            status_code=400, detail=f"{item.filename} 不是 VTK"
                        )
                    dest = _unique_dest(
                        turb_path, _safe_name(item, f"turbulence_{i}.vtk"), i
                    )
                    await _save_upload(item, dest)
                turb_tiles = len(turbulence)
            turb_resampling = "nearest"
            if manifest is not None:
                turb_path = tmp_path / "turbulence_subset"
                turb_tiles = await _save_turbulence_subset(manifest, turbulence_subset, turb_path)
                turb_resampling = str(manifest.get("resampling") or "nearest")

            from stfusion.config import config_from_cli
            from stfusion.io import inspect_info
            from stfusion.pipeline import fuse_to_vtk

            output = tmp_path / "fused.vtk"
            cfg = config_from_cli(
                vtk_path,
                pairs,
                output,
                target_crs=target_crs.strip() or "EPSG:32649",
                vtk_crs=vtk_crs.strip() or "EPSG:32649",
                strategy=strategy,
                resampling=resampling,
                wrf=wrf_path,
                wrf_field=(wrf_field or wrf_var or "QVAPOR").strip() or "QVAPOR",
                wrf_var=(wrf_var or "QVAPOR").strip() or "QVAPOR",
                turbulence=turb_path,
                turbulence_resampling=turb_resampling,
            )
            result = await run_in_threadpool(fuse_to_vtk, cfg)
            meta = await run_in_threadpool(inspect_info, result)
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            saved_name = f"{_safe_output_stem(vtk.filename or 'fused')}_fused.vtk"
            saved = OUTPUT_DIR / saved_name
            await run_in_threadpool(shutil.copy2, result, saved)
            sidecar = {
                "fields": meta.get("fields") or [],
                "source": Path(vtk.filename or "").name,
                "vtk_crs": cfg.vtk_crs,
                "turbulence_tiles": turb_tiles,
            }
            saved.with_suffix(".json").write_text(
                json.dumps(sidecar, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return JSONResponse({"item": _item_from_vtk(saved)})
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await run_in_threadpool(shutil.rmtree, tmp, ignore_errors=True)

    @app.get("/api/outputs")
    def list_outputs() -> dict:
        return {"items": _list_outputs()}

    @app.get("/api/outputs/{name}")
    def download_output(name: str) -> FileResponse:
        path = _output_vtk_path(name)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="找不到该 VTK 文件")
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/octet-stream",
        )

    @app.delete("/api/outputs/{name}")
    def delete_output(name: str) -> dict:
        path = _output_vtk_path(name)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="找不到该 VTK 文件")
        sidecar = path.with_suffix(".json")
        path.unlink()
        if sidecar.is_file():
            sidecar.unlink()
        return {"ok": True, "name": path.name}

    return app


def _require_fusion() -> None:
    status = fusion_status()
    if not status["ready"]:
        raise HTTPException(status_code=503, detail=_missing_message(status["missing"]))


def _missing_message(missing: list[str] | None = None) -> str:
    names = missing or fusion_status()["missing"]
    return (
        "融合依赖尚未安装："
        + "、".join(names)
        + "。请在项目目录执行 python -m pip install -e ."
    )


def _unique_dest(folder: Path, filename: str, index: int) -> Path:
    dest = folder / filename
    if not dest.exists():
        return dest
    return folder / f"{dest.stem}_{index}{dest.suffix}"


def _copy_upload(upload: UploadFile, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    upload.file.seek(0)
    with dest.open("wb") as handle:
        shutil.copyfileobj(upload.file, handle, 16 * 1024 * 1024)


async def _save_upload(upload: UploadFile, dest: Path) -> None:
    try:
        await run_in_threadpool(_copy_upload, upload, dest)
    finally:
        await upload.close()


async def _save_turbulence_subset(
    manifest: dict,
    uploads: list[UploadFile],
    folder: Path,
) -> int:
    """把网页端上传的切片子集写成 load_subset_tiles 能读的目录，返回切片数。"""
    from stfusion.turbulence import SUBSET_MANIFEST

    by_name = {Path(item.filename or "").name: item for item in uploads}
    folder.mkdir(parents=True, exist_ok=True)
    tiles = manifest["tiles"]
    for i, tile in enumerate(tiles):
        if not isinstance(tile, dict):
            raise HTTPException(status_code=400, detail="turbulence_manifest 格式错误")
        tile.pop("data_file", None)
        name = Path(str(tile.get("name") or "")).name
        upload = by_name.pop(name, None)
        if upload is None:
            continue
        data_file = f"{i:03d}.bin"
        await _save_upload(upload, folder / data_file)
        tile["data_file"] = data_file
    if by_name:
        raise HTTPException(
            status_code=400,
            detail="以下切片子集不在清单中：" + "、".join(sorted(by_name)),
        )
    (folder / SUBSET_MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return len(tiles)


app = create_app()
