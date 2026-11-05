# 时空数据融合（VTK + GeoTIFF + WRF 湿度 + 风切变/湍流）

把三维气象 VTK 当目标网格，把人口 GeoTIFF、WRF `wrfout` 湿度、风切变/湍流切片采到同一套网格上，输出新的 VTK。

前端不是单独的 npm 项目。页面在 `src/stfusion/web/static/`，由同一个 Python 服务一起启动。

## 启动前端（网页）

第一次（只需做一次）：

```powershell
cd "D:\Drone\Spatiotemporal Data Fusion"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

`pip install -e .` 是把本项目以开发模式装进虚拟环境，之后才能运行 `python -m stfusion`。改源码一般不用再装。

以后每次启动：

```powershell
cd "D:\Drone\Spatiotemporal Data Fusion"
.\.venv\Scripts\Activate.ps1
python -B -m stfusion
```

终端出现监听提示后，浏览器打开：

**http://127.0.0.1:3006/**

这就是前端页面。选气象 VTK，可选人口 GeoTIFF、WRF `wrfout` 和风切变/湍流 VTK，点「开始融合」。结果在项目 `output/` 目录。

风切变/湍流是多块 4m 切片：第 4 步可一次多选、选整个文件夹，或拖入文件夹。

湍流切片每块约 1GB，但 64m 气象网格只用到其中极少的格点。网页不会上传整个切片：浏览器先在本地读取各切片的 VTK 头，向服务端要到"需要哪些字节"，再只截取这些字节上传（安阳 7 块共 7.5GB，实际上传约 30MB，融合几秒完成），结果与上传完整切片逐位一致。气象 VTK 不是规则网格（ImageData）或切片格式不支持时，自动退回上传完整切片。

改端口：

```powershell
python -B -m stfusion --host 0.0.0.0 --port 9000
```

装好后也可以直接用命令 `stfusion` 启动同一个服务。

## 当前规则

- **叠加方式**：在原始气象 VTK 上加入人口、湿度、风切变和湍流等属性，**不改网格、不拆矢量场**。
- **网格**：输出与气象 VTK 完全相同（Dimensions / Spacing / Origin）。
- **人口**：重投影到气象坐标系后按 XY 取样，再沿 Z 复制到每一层。
- **湿度**：读取 WRF 的 `QVAPOR`（三维水汽混合比），写入 VTK 时字段名仍为 `QVAPOR`。用 `XLAT`/`XLONG` 转到 VTK 的平面坐标，用 `(PH+PHB)/g − HGT` 得到距地高度，再插值到气象网格的每一层；**不沿 Z 复制**。如果无法读取垂直高度，则使用最低层并沿 Z 复制。
- **风切变 / 湍流**：读取 4m 切片 VTK（`HorizontalShear`、`VerticalShear`、`Epsilon`，以及可选 `AirMask`），按坐标抽样到气象网格。与安阳 64m 气象场原点对齐、分辨率恰好 16:1 时取共点；切片按 1 个格点重叠拼接；没有切片覆盖的格点为 NaN。
- **气象场**：原样保留（例如 `velocity` 仍是一个三分量矢量）。新字段与已有字段重名时，会自动加上数据源名称作为前缀。
- **坐标系**：气象 VTK 固定为 **EPSG:32649（WGS 84 / UTM 49N）**。人口 GeoTIFF 用文件自带 CRS（安阳人口为 EPSG:4326）。湿度 `XLAT`/`XLONG` 按经纬度转到 UTM 49N。输出坐标与原始气象 VTK 一致。
- **网格策略**：`coarsest` / `finest` 参数目前为兼容配置保留；实际融合始终保持 VTK 原始网格。

## HTTP API

网页服务默认基地址为 `http://127.0.0.1:3006`，上传接口使用 `multipart/form-data`。网页为 `GET /`，静态资源为 `/static/`。通过 Nginx 的 `/fuse/` 前缀访问时，公开地址对应为 `/fuse/`、`/fuse/api/` 和 `/fuse/static/`。

### `GET /api/health`

响应：

```json
{"ready": true, "missing": []}
```

`ready` 表示依赖是否就绪；缺失包名列在 `missing` 数组。

### `POST /api/inspect`

表单字段 `file`（必填文件）支持 VTK、GeoTIFF、湍流 VTK 或 WRF `wrfout`：

```bash
curl -F "file=@data/weather.vti" http://127.0.0.1:3006/api/inspect
```

成功时按文件类型返回元信息。VTK 含 `type`、`name`、`points`、`cells`、`bounds`、`fields`、`looks_projected`；`ImageData` 另有 `dimensions`、`origin`、`spacing`。GeoTIFF 含 `crs`、`width`、`height`、`bands`、`dtype`、`bounds`、`res`、`nodata`。WRF 含 `var`、`west_east`、`south_north`、`bottom_top`、`lat`、`lon`、`dx`、`has_height`。湍流含 `tiles`、`fields`、`spacing`、`bounds`。类型不支持或解析失败返回 `400`，依赖缺失返回 `503`。

### `POST /api/turbulence/plan`

JSON 请求体，给出气象网格和各湍流切片的头（结构同 `stfusion.vtk_legacy.parse_structured_points` 的返回值），返回每块切片需要上传的字节区间：

```json
{
  "grid": {"origin": [800100, 3991320, 0], "spacing": [64, 64, 64], "dimensions": [113, 246, 5]},
  "resampling": "nearest",
  "tiles": [
    {"name": "anyang_0000-0000_turbulence.vtk", "size": 1075895203,
     "header": {"dimensions": [901, 982, 76], "origin": [800100, 3991320, 0], "spacing": [4, 4, 4],
                "n_points": 67243432, "fields": [{"name": "HorizontalShear", "ncomp": 1, "nvals": 67243432, "dtype": "float", "offset": 206}]}}
  ]
}
```

响应含 `manifest`（融合时原样作为 `turbulence_manifest` 提交）、`transfers`（每块的 `ranges`：`[起始字节, 长度]` 列表，以及 `nbytes`）和 `total_bytes`。按顺序截取 `ranges` 拼成一个文件，以切片文件名作为 `turbulence_subset` 上传；`ranges` 为空的切片不用上传。

### `POST /api/fuse`

`vtk`（文件）必填，`geotiff`（文件，可重复）、`wrf`（文件）、`turbulence`（文件，可重复）或 `turbulence_manifest` + `turbulence_subset` 至少一个；其余表单字段：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `geotiff_field`（可重复） | 第一个 `population`，其余文件名 | 与 GeoTIFF 按顺序对应。 |
| `wrf_var` | `QVAPOR` | WRF 变量；缺失时尝试 `Q2`。 |
| `wrf_field` | `wrf_var` | 写入 VTK 的字段名。 |
| `turbulence`（可重复） | 空 | 多个完整风切变/湍流 VTK 切片，一次可传全部。 |
| `turbulence_manifest` | 空 | `/api/turbulence/plan` 返回的 `manifest`（JSON 字符串）。与 `turbulence` 二选一。 |
| `turbulence_subset`（可重复） | 空 | 按 `ranges` 截取的切片子集，文件名为切片原名。子集没覆盖到气象网格所需格点时返回 `400`。 |
| `vtk_crs` | `EPSG:32649` | 气象 VTK CRS，默认 UTM 49N。 |
| `target_crs` | `EPSG:32649` | 兼容字段，输出最终跟随 VTK CRS。 |
| `strategy` | `coarsest` | `coarsest` 或 `finest`。 |
| `resampling` | `bilinear` | `bilinear` 或 `nearest`。 |

请求示例：

```bash
curl -X POST \
  -F "vtk=@data/weather.vti" \
  -F "geotiff=@data/population.tif" \
  -F "geotiff_field=population" \
  -F "vtk_crs=EPSG:32649" \
  http://127.0.0.1:3006/api/fuse
```

成功返回 `200`，响应为 `{"item": {...}}`，`item` 字段如下：

```json
{
  "name": "weather_fused.vtk",
  "size": 123456,
  "size_text": "120.56 KB",
  "mtime": "2026/9/5 10:30:00",
  "fields": ["velocity", "population", "QVAPOR", "HorizontalShear", "VerticalShear", "Epsilon"],
  "source": "weather.vti",
  "vtk_crs": "EPSG:32649"
}
```

结果写入项目 `output/` 目录，文件名为上传 VTK 文件名（去扩展名）加 `_fused.vtk`，同名会覆盖。参数或融合错误返回 `400`，依赖缺失返回 `503`，缺少必填 multipart 字段返回 `422`。

### `GET /api/outputs`

按修改时间倒序列出输出：

```json
{"items": [{"name": "weather_fused.vtk", "size": 123456, "size_text": "120.56 KB", "mtime": "2026/9/5 10:30:00", "fields": ["population"], "source": "weather.vti", "vtk_crs": "EPSG:32649"}]}
```

### `GET /api/outputs/{name}`

下载指定输出 VTK，响应类型 `application/octet-stream`。文件不存在返回 `404`，扩展名或文件名不合法返回 `400`。

### `DELETE /api/outputs/{name}`

删除指定 VTK 及同名 JSON 元数据文件。成功响应：

```json
{"ok": true, "name": "weather_fused.vtk"}
```

文件不存在返回 `404`，扩展名或文件名不合法返回 `400`。

## 支持的文件扩展名

| 类型 | 扩展名 |
| --- | --- |
| VTK | `.vtk`、`.vti`、`.vtp`、`.vts`、`.vtu`、`.vtr` |
| GeoTIFF | `.tif`、`.tiff`、`.geotiff` |
| WRF | `.nc`、`.nc4`、`.cdf`；无扩展名但文件名包含 `wrfout` 时网页也可识别。 |
| 风切变/湍流 | `.vtk` 切片文件（网页可一次多选、选文件夹或拖入文件夹）。 |
