# Windows 桌面应用

开发启动：

```powershell
python desktop_app.py
```

设置、运行历史、OpenAI 模型缓存、防重账本和 DPAPI 加密凭据存放在当前 Windows 用户的 `%LOCALAPPDATA%\UPCAM\TaskDigest`。

应用包含以下功能：

- 配置 Teambition Tenant/Project、App 凭据、钉钉机器人、OpenAI API Key、模型、reasoning effort 和服务等级。
- 手动刷新并本机缓存当前 OpenAI API Key 可见的模型；启动时只读缓存，不自动请求模型列表。
- 配置负责人/状态映射和项目视图核验信息；日报格式从程序随附的 `templates/daily_digest.json` 自动读取。
- 配置上午/下午调度及各自的采集窗口，执行 Teambition 只读连接测试，查看运行/发送状态。
- 日报通过本地规则后无需逐次人工审核，程序直接生成摘要并发送钉钉。结果不明时阻断重试。

自动调度只在此应用保持运行且 Windows 未休眠期间工作；应用关闭后不会补跑错过的时间点。

自动摘要只把伪匿名任务键、任务标题和需要摘要的评论正文交给 OpenAI。筛选结果为空且采集覆盖完整时，应用按模板输出“经完整采集，筛选时间段内未发现任务动态”，不调用 OpenAI。采集不完整、事件未知、视图或映射未核验及动态覆盖不确定时阻断发送。

OpenAI API 默认使用显式标准服务等级 `default`。Fast 是 Responses API 的 `service_tier=fast`，需要用户主动选择；可能产生更高 token 费用，不保证更快，项目/账户/区域权限未必允许。只对程序已核验支持 Fast 的模型开放请求。

本次验证只运行了基于虚构 fixtures 的本地单元测试。没有使用真实密钥或访问 Teambition、OpenAI、钉钉接口；没有构建 Windows EXE，也没有部署到目标电脑。手动 EXE 构建入口在 `.github/workflows/windows-build.yml`。
