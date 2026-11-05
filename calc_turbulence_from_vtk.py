"""从 VTK 风场计算湍流参数并保存为 VTK。

输入: vtkStructuredPoints/StructuredGrid，含矢量速度场（velocity, 3 分量）
      （参考 hevc_comression.get_vtk_data 的读取方式）
处理:
  1. 读取 velocity 矢量 (X,Y,Z,3)，转 (Z,Y,X,3) numpy 布局
  2. 由速度分量全零判定建筑掩膜（空气=1）
  3. 计算:
       - 垂直风切变  VWS = |∂V/∂z|  (√[(∂u/∂z)²+(∂v/∂z)²])
       - 水平风切变  HWS = √[(∂u/∂x)²+(∂v/∂x)²+(∂u/∂y)²+(∂v/∂y)²]
       - 涡旋耗散率 EPS（Kolmogorov 二阶结构函数法，立方根压缩，
         与 turbulence.compute_turbulence 一致）
  4. 保存为 VTK（STRUCTURED_POINTS + 3 个标量场 + 掩膜）

用法:
  python calc_turbulence_from_vtk.py <input.vtk> [output.vtk]
"""

import os
import sys
import time

import numpy as np
import vtk
from vtk.util import numpy_support

# 复用湍流计算（避免重复实现）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from turbulence import (compute_turbulence, compute_vertical_shear,
                        compute_horizontal_shear)


# ============================================================
# VTK 读写
# ============================================================

def read_vtk_wind(path):
    """读取 VTK 风场。

    Returns:
        u, v, w: (Z, Y, X) float32 风速分量
        dims: (nx, ny, nz) VTK 顺序
        spacing: (dx, dy, dz)
        origin: (x0, y0, z0)
    """
    reader = vtk.vtkStructuredPointsReader()
    reader.SetFileName(path)
    reader.Update()
    data = reader.GetOutput()

    dims = data.GetDimensions()          # (nx, ny, nz)
    spacing = data.GetSpacing()
    origin = data.GetOrigin()

    pd = data.GetPointData()
    vec = pd.GetVectors()
    if vec is None:
        # 退而求其次：找 3 分量数组
        for i in range(pd.GetNumberOfArrays()):
            a = pd.GetArray(i)
            if a and a.GetNumberOfComponents() == 3:
                vec = a
                break
    if vec is None:
        raise ValueError(f'未找到 3 分量速度场: {path}')

    np_vec = numpy_support.vtk_to_numpy(vec)
    print(f'  原始数组形状: {np_vec.shape}  类型: {np_vec.dtype}')

    # VTK 展开顺序 x 最快（x 内层、z 外层）→ reshape 直接得到 (Z, Y, X, 3)
    # （与 hevc_comression.get_vtk_data 一致）
    np_vec = np_vec.reshape(dims[2], dims[1], dims[0], 3).copy()

    u = np_vec[..., 0].astype(np.float32)
    v = np_vec[..., 1].astype(np.float32)
    w = np_vec[..., 2].astype(np.float32)
    return u, v, w, dims, spacing, origin


def save_fields_vtk(fields, origin, spacing, save_path):
    """将 (Z,Y,X) numpy 场保存为 STRUCTURED_POINTS VTK。"""
    first = next(iter(fields.values()))
    nz, ny, nx = first.shape
    grid = vtk.vtkStructuredPoints()
    grid.SetDimensions(nx, ny, nz)
    grid.SetSpacing(*spacing)
    grid.SetOrigin(*origin)
    pd = grid.GetPointData()
    for name, arr in fields.items():
        vtk_arr = numpy_support.numpy_to_vtk(
            arr.ravel(), deep=True, array_type=vtk.VTK_FLOAT)
        vtk_arr.SetName(name)
        pd.AddArray(vtk_arr)
    writer = vtk.vtkStructuredPointsWriter()
    writer.SetFileName(save_path)
    writer.SetInputData(grid)
    writer.SetFileTypeToBinary()
    writer.Write()
    print(f'VTK 已保存: {save_path}')


# ============================================================
# 主流程
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    in_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else \
        os.path.splitext(in_path)[0] + '_turbulence.vtk'

    # 自动处理输出路径：vtkStructuredPointsWriter 在 Windows 上无法写入
    # 含非 ASCII 字符的路径（中文目录），检测到则改写为 ASCII 临时目录
    out_path = ensure_ascii_output_path(out_path, in_path)

    t0 = time.time()
    print(f'读取: {in_path}')
    u, v, w, dims, spacing, origin = read_vtk_wind(in_path)
    res = float(spacing[0])   # 网格分辨率（水平），算法用统一 res

    # ---- 空气掩膜：三分量全为 0 视为建筑/无效 ----
    mask = ((np.abs(u) + np.abs(v) + np.abs(w)) > 0).astype(np.float32)
    print(f'  数据形状 (Z,Y,X): {u.shape}  空气占比: {mask.mean():.3f}')

    # ---- 湍流参数 ----
    print('计算湍流参数...')
    _, eps, L, u_mean, v_mean = compute_turbulence(u, v, w, mask, res)
    vws = compute_vertical_shear(u, v, u_mean, v_mean, mask, spacing[2])
    hws = compute_horizontal_shear(u, v, u_mean, v_mean, mask,
                                   spacing[0], spacing[1])

    # ---- 保存 ----
    save_fields_vtk(
        {'HorizontalShear': hws, 'VerticalShear': vws, 'Epsilon': eps,
         'AirMask': mask},
        origin, spacing, out_path)

    print(f'\n完成，总耗时 {time.time()-t0:.1f} 秒')
    print(f'场范围:  HWS [{hws.min():.3f}, {hws.max():.3f}]  '
          f'VWS [{vws.min():.3f}, {vws.max():.3f}]  '
          f'EPS [{eps.min():.3f}, {eps.max():.3f}]')


def ensure_ascii_output_path(out_path, in_path=None):
    """确保输出路径为纯 ASCII（vtk writer 无法写中文路径）。

    若路径含非 ASCII 字符，则改写为:
      <输入文件所在 ASCII 目录或系统临时目录>/<纯 ASCII 文件名>
    并在返回路径后由调用方打印提示。
    """
    try:
        out_path.encode('ascii')
        return out_path          # 已是 ASCII，直接用
    except UnicodeEncodeError:
        pass

    # 从输入文件名构造 ASCII 输出名（保留 rank 信息便于识别）
    base = os.path.basename(in_path) if in_path else 'output'
    stem = os.path.splitext(base)[0]
    # 去掉可能的中文/非 ASCII，保留 ascii 部分
    ascii_stem = ''.join(c for c in stem if ord(c) < 128)
    fname = ascii_stem + '_turbulence.vtk'

    # 优先使用输入文件所在盘符的 ASCII 目录（如 E:\），否则用系统临时目录
    candidates = []
    if in_path:
        drive = os.path.splitdrive(os.path.abspath(in_path))[0]
        if drive:
            candidates.append(os.path.join(drive + os.sep, fname))
    import tempfile
    candidates.append(os.path.join(tempfile.gettempdir(), fname))

    for c in candidates:
        try:
            c.encode('ascii')
            return c
        except UnicodeEncodeError:
            continue
    return os.path.join(tempfile.gettempdir(), fname)


if __name__ == '__main__':
    main()
