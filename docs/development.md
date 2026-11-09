# 开发与验证

## 运行

需要 Python 3.12 或更高版本。使用明确的解释器创建专用环境；可用 uv 按 uv.lock 安装，也可用 pip 安装带哈希锁定的运行依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

前端使用 Node.js。构建会把静态文件写入 Python 包的 web/dist，运行时不需要 Node：

```powershell
cd frontend
npm ci
npm test
npm run build
cd ..
.\.venv\Scripts\python.exe -m yuecang --project D:\Books\Development serve
```

## 检查

安装开发依赖后执行 `python -m pytest`。测试全用临时目录和合成材料，涵盖内容哈希、来源区间、并发编辑、提案校验、任务租约、格式安全、导出、备份恢复和缓存保护。

`tests/ui/acceptance.py` 使用 Playwright 对已运行的本机服务执行交互流程；参数见脚本帮助。需要独立空书库与浏览器环境。实际浏览器验收和 HTTP 模拟模型测试应分别记录，模拟服务不能冒充真实模型。

`tests/luna_scenario.py` 生成合成离线审阅请求并校验返回提案，用于有明确授权的真实模型验收。它不会调用云服务或读取现有书库。

`tests/test_encoding_recovery.py`、`test_source_analysis.py`、`test_v020_integration.py` 覆盖编码恢复、混书提醒、同版对齐和请求持久化。`tests/test_legacy_doc.py` 包含真实 LibreOffice 转换用例及进程生命周期检查；缺少平台或转换器时会明确跳过，应单独报告未验证范围。`tests/ui/v020_acceptance.py` 可自启一次性服务，使用现有 Playwright 与 LibreOffice 验证新功能界面，执行参数见 `--help`。测试仅生成合成书籍，不下载真实图书。

## 打包

`tools/build_portable.py` 使用显式指定的已有 Windows CPython 运行时，按锁文件构建自包含目录。输出必须为新的目录；不复用维护者私有环境，不自动下载 Python，不从源码目录外寻找用户资料。打包前先构建前端，详见脚本 `--help`。

发布候选应从仓库外启动，以包内 Python 验证安装、原生依赖、工作台、导入导出及恢复。构建、自动测试、交互验收和对外发布是不同事实；运行成功不代表已发布。

## 修改原则

保持单体服务与少量清晰模块，不引入不需要的消息队列或数据库服务器。耗时工作交给持久任务队列，网络请求与解析不占数据库写事务。源码变更应有风险对应的测试；仅文档调整无需重跑全部验收。
