# 接入较新的原 OAS 调度器

本功能用于用户自己的 `evan5791/OnmyojiAutoScript` 分支，不向上游提交 PR。

若安装目录的 `script.py` 已使用 `ScriptRuntimeController`，请保留该文件并按以下位置接线，不能用旧 dev 的整份 `script.py` 覆盖。保留原有 `ensure_image_server_ready()`、`ensure_ocr_server_ready()`、协战及设备管理入口；此功能不新增 OCR 服务。

1. 导入 `from tasks.AccountSwitch import service as account_service`。`loop()` 启动后、第一次 `get_next_task()` 和运行时设备准备之前，调用 `account_service.arm_service(self.config, logger=logger)`。只在服务启动时调用，不能每轮重新推迟计划。
2. `get_next_task()` 已确定 `self.config.task` 且任务尚未到点时，在调用 `runtime.handle_wait_during_idle()` 前检查 `account_service.preserve_vm(self.config)`；或尚未缓存 `device` 且 `account_service.initial_future_wait(self.config)`。符合任一条件直接 `wait_until(task.next_run)`；被配置更新中断则清除 Config 缓存并重新调度。不调用运行时关闭、庭院导航或预热策略。
3. `run()` 开始执行其他任务时调用 `account_service.clear_outcome(self.config)`；小号任务开始时保留其已持久化的结果。沿用原方法重置本次临时 outcome。
4. `_handle_task_exception()` 对 `AccountSwitch` 先尝试 `account_service.handle_failure(self.config, e, logger=logger)`；返回 True 时捕获 outcome 并视作本次已处理，不进入通用重启。此接口不接管停止信号、人工接管或编程错误，其他任务继续使用原恢复逻辑。
5. 小号 `TaskEnd` 捕获 outcome，以及 `run()` 的 `finally` 中保存 outcome：通过原 `_capture_task_runtime_outcome()` 接入 `account_service.remember_outcome(self.config)`，使等待状态跨 Config 重载保留。保持原导航配置的 finally 恢复逻辑。
6. 原有“失败次数超限后停止脚本”分支，仅在 `not account_service.preserve_vm(self.config)` 时执行 `emulator_stop()`；脚本仍可按原规则停止，失败现场保留。

任务到点后的环境准备仍归原 `ScriptRuntimeController`；设备构造阶段的人工接管或原程序退出规则保持原样。只对已知的小号任务页面、截图、ADB 异常安排原刷新窗口内的重试。共享 OCR 是否预热继续取决于原壳配置，不应将“等待不额外初始化设备”理解成整个原壳完全不启动 OCR。

可通过环境变量 `OAS_ACCOUNT_SWITCH_COMPAT_SCRIPT` 指向待验证的新版 `script.py`，运行 `python -m unittest tasks.AccountSwitch.test_compat_runtime`。测试只解析指定文件的 AST，用 stub 执行改动方法；不导入其顶层设备/OCR模块，不操作真实设备。未设置变量且当前 checkout 是旧调度器时，此组测试跳过；旧壳由 `test_service.py` 覆盖。
