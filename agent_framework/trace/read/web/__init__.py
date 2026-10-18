"""Agent Tracer：观测数据的只读可视化网页。

页面结构与视觉沿用上一版 PCBAgent Tracer（列表 → Run → Stage → Agent → LLM），
实现为 FastAPI 服务端页面 + 只读 JSON API，运行不需要 Node。

```python
from agent_framework import trace

trace.init(connect, dialect="mysql")     # 落库
trace.run(connect, port=8765)            # 起网页
```

公共入口 ``trace.run()`` 可以复用 ``trace.init()`` 的连接；直接调用本包的
``run()`` / ``serve()`` 时必须显式传连接，读侧不会反向依赖写入运行时。

网页**只读**：整个 web 包只使用 ``read.TraceReader``，不碰任何 writer。
"""

from .app import create_app
from .server import run, serve

__all__ = ["create_app", "run", "serve"]
