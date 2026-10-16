"""连接配置：默认走本机 socket，可用环境变量切到远端共享账号。

默认值对应《数据库信息.txt》：目标库 device_parameters_dev。
本机 socket 用 pcb_dev（空密码，仅本机开发用）；远端共享账号 datas_rw 需要密码，
密码从环境变量 DEVICE_SELECTION_DB_PASSWORD 读入，不写进代码，也不打印。
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping

DEFAULT_SOCKET = "/share/home/chenxin/my-database/run/mysql.sock"
DEFAULT_DATABASE = "device_parameters_dev"

# 官方文档确认的两个远端入口
DEFAULT_REMOTE_HOSTS = ("10.131.254.102", "10.0.0.102")
DEFAULT_REMOTE_PORT = 3308


def _env(environ: Mapping[str, str], name: str, default: str) -> str:
    raw = environ.get(name)
    value = str(default if raw is None or not str(raw).strip() else raw).strip()
    return value


@dataclass(frozen=True, slots=True)
class DBConfig:
    """一份只读连接描述。密码不落在对象上，避免被打印或序列化。"""

    mode: str  # "socket" | "tcp"
    database: str
    user: str
    socket_path: str = ""
    host: str = ""
    port: int = DEFAULT_REMOTE_PORT
    password_env: str = "DEVICE_SELECTION_DB_PASSWORD"
    connect_timeout_s: int = 8
    query_timeout_ms: int = 20_000

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "DBConfig":
        env = os.environ if environ is None else environ
        mode = _env(env, "DEVICE_SELECTION_DB_MODE", "socket").lower()
        database = _env(env, "DEVICE_SELECTION_DB_NAME", DEFAULT_DATABASE)
        if mode == "tcp":
            return cls(
                mode="tcp",
                database=database,
                user=_env(env, "DEVICE_SELECTION_DB_USER", "datas_rw"),
                host=_env(env, "DEVICE_SELECTION_DB_HOST", DEFAULT_REMOTE_HOSTS[0]),
                port=int(_env(env, "DEVICE_SELECTION_DB_PORT", str(DEFAULT_REMOTE_PORT))),
            )
        if mode != "socket":
            raise ValueError("DEVICE_SELECTION_DB_MODE must be 'socket' or 'tcp'")
        return cls(
            mode="socket",
            database=database,
            user=_env(env, "DEVICE_SELECTION_DB_USER", "pcb_dev"),
            socket_path=_env(env, "DEVICE_SELECTION_DB_SOCKET", DEFAULT_SOCKET),
        )

    def connect_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "user": self.user,
            "database": self.database,
            "password": os.environ.get(self.password_env, ""),
            "connection_timeout": self.connect_timeout_s,
            "autocommit": False,
        }
        if self.mode == "socket":
            kwargs["unix_socket"] = self.socket_path
        else:
            kwargs["host"] = self.host
            kwargs["port"] = self.port
        return kwargs

    def describe(self) -> dict[str, object]:
        """可安全打印的连接说明——不含密码。"""

        if self.mode == "socket":
            endpoint = self.socket_path
        else:
            endpoint = f"{self.host}:{self.port}"
        return {
            "mode": self.mode,
            "endpoint": endpoint,
            "database": self.database,
            "user": self.user,
            "needs_password": self.mode == "tcp",
        }
