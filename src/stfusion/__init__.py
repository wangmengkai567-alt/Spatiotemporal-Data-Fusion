"""将气象 VTK 与 GeoTIFF、WRF 湿度、风切变/湍流叠加到同一套网格上。"""

import os
import sys

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

__version__ = "0.1.0"
__all__ = ["__version__"]
