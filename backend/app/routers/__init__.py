"""业务路由汇总。

结算相关的固定路径必须先于通用 ``/api/{module}`` 注册。
"""
from __future__ import annotations
from app.routers import settlement, resource
ROUTERS = [settlement, resource]
