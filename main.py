"""
wxauto 4.1 自动回复监控系统
前提：启动前请先手动登录微信 PC 客户端
"""
import sys
import os

# 确保能导入 wxauto
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auto_reply import MonitorEngine, AutoReplyUI


def main():
    engine = MonitorEngine(poll_interval=3)
    ui = AutoReplyUI(engine)
    ui.build_and_run()


if __name__ == "__main__":
    main()
