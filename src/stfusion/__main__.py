import argparse
import os
import sys

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True


def main() -> None:
    parser = argparse.ArgumentParser(description="启动时空数据融合 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=3006, help="端口")
    args = parser.parse_args()

    import uvicorn
    from stfusion.web.app import create_app

    print(f"请在浏览器打开 http://{args.host}:{args.port}/")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info", timeout_keep_alive=300)


if __name__ == "__main__":
    main()
