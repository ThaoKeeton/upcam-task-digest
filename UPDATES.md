# 私有仓库稳定更新

## 用户操作

1. 打开 **常规 → 版本更新**。唯一更新源为 `ThaoKeeton/upcam-task-digest` 的正式稳定 GitHub Release，不读取其他仓库、任意网址、源码压缩包或临时 Actions artifact。
2. 此仓库为私有仓库。在自己的 GitHub 账户创建仅能读取该仓库 **Contents: Read** 的 fine-grained token，在应用内输入后保存。组织若要求审批或 SSO，需先满足其访问条件。不要把 OpenAI Key、Teambition 密钥或钉钉 Webhook 填在这里。
3. 令牌仅存入当前 Windows 用户 DPAPI 凭据文件，输入框不会回显原值；留空保留。“清除更新令牌”在保存后生效，不影响其他服务凭据。更新功能不要求填写或调用日报项目配置。
4. 点击“检查更新”。权限不足、尚无稳定发布、网络失败、无可验证完整包与当前无须更新分别显示；无法核验时不会显示“已是最新版”。
5. 新版本可用时点击“下载并校验更新”。所有校验成功后，才可点击“安装更新并重新启动”。请先保存其他页面的编辑；正在运行的任务不会被更新按钮中断。
6. 新版本安装在 `%LOCALAPPDATA%\UPCAM\TaskDigest\updates\versions`，使用原有配置、凭据、缓存和历史。旧目录不删除；新进程等待旧进程退出再取得单实例锁。新工作进程成功初始化后，才将已启用的用户登录启动项改为新路径。若修复启动项失败，常规页保持启动开启并重新保存即可重试。

界面支持 Windows 默认、简体中文、English；主题和托盘不需要更新令牌即可使用。更新检查默认手动，自动检查可选，不会自动下载或安装。

## 验证边界

- HTTPS API、仓库、Release URL、资产 API 与资产文件名固定；仅允许稳定 `vMAJOR.MINOR.PATCH`、完整40位提交和 GitHub 返回的 SHA-256 digest。缺摘要或元数据冲突即停止。
- 下载资产重定向只能到受限 GitHub 资产路径，跳转后不转发 Authorization。错误不会回显令牌、响应正文或带签名的下载 URL。
- 下载≤512MiB，展开≤1.5GiB，文件≤10000，单文件≤512MiB，压缩比例≤200，整次操作≤300秒。中央目录在分配解压对象前检查。
- 拒绝绝对路径、上级跳转、NTFS ADS、Windows 保留名、大小写别名、重复路径、链接、特殊文件及不完整应用。
- 随包 `build-info.json` 必须与发布版本、完整提交以及 app/worker 实际 SHA-256 一致。准备启动前重新认证 Release、复算 ZIP digest 并逐文件核对已解压目录；不执行半包。
- 不覆盖运行目录、不请求管理员提权、不绕过目录权限。文件系统不安全或不可写时保留原应用并停止。
- 上述是固定仓库 HTTPS 来源和摘要完整性校验，**不是 Authenticode 代码签名**。仓库维护者与 GitHub 账户安全仍属于信任边界。离线 `verify_installed` 仅检查已保存包与目录一致，不能替代首次 Release 来源认证。
- 保留旧目录供手动回退，不声称实现通用自动回滚。干净 Windows、不同安全软件及真实高 DPI 显示器需要目标环境验收。

## 维护者发布

在 GitHub Actions 打开 **Windows desktop build → Run workflow**，分支选 **main**，仅在确实要发布时勾选 **publish_stable**。默认不发布；PR 构建永远不发布。

流程先完成核心测试、WinUI/旧版构建、真实原生控件测试和完整 ZIP 解压/启动测试，再验证 ZIP 摘要与 source/version/rules 元数据。随后创建草稿 Release，校验 GitHub 资产 digest 后才发布稳定版。已有 Release 不覆盖；已有 tag 指向不同提交也拒绝。发布步骤仅在人工触发的 main 构建拥有 Contents 写权限；普通构建保持只读。

仓库目前没有正式 Release，首个 Release 发布前无法完成真实远端升级验收。开发测试使用不可执行的合成包和注入传输，不读取真实 GitHub token、不创建真实 Release、不发送日报。安装交接、语言/主题和托盘由 Windows CI 的隔离模式验证，实际证据随构建产物交付。

参考：[GitHub Releases API](https://docs.github.com/en/rest/releases/releases)、[GitHub 个人访问令牌](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)。
