#!/bin/sh
# 通过绝对路径启动智灵运行时 venv，避免登录 shell 重设 PATH 后裸 `python`
# 退回系统 Python，从而找不到 WebUI 已安装依赖。
exec /opt/zhiling/.venv/bin/python "$@"
