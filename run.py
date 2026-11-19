"""PyInstaller 打包入口：启动本地 FastAPI 服务（127.0.0.1:8799）。"""

import uvicorn

from backend.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8799, log_level="info")
