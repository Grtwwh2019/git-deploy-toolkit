# Git Deploy Toolkit

从两个 Git Commit 生成增量 ZIP，并在 Linux 上按文件校验、备份、部署及手动回滚。支持 GitHub、GitLab 或其他标准 Git Remote。Windows 脚本将 Push 和打包串联起来；Linux 部署不需要安装 Git。

## 环境与文件

- Windows：Git、Python 3.9+（`python` 命令）、PowerShell 5.1+。
- Linux：Python 3.9+（`python3` 命令）、Bash、unzip；执行用户需要目标目录和发布目录的读写权限。
- Python 代码仅使用标准库，无需 `pip install`。

| 文件 | 用途 |
| --- | --- |
| `push_and_package.ps1` | 检查工作区，Push 后自动生成增量包 |
| `package.ps1` | 已经 Push 后，根据两个 Commit 手动打包 |
| `package.py` | 读取 Git 对象并生成增量 ZIP |
| `deploy.sh` | Linux 部署入口 |
| `rollback.sh` | Linux 回滚入口 |
| `deploy_engine.py` | 文件校验、备份、部署状态及回滚实现 |
| `.gitignore` | 排除生成包、备份、部署状态和常见本地配置 |
| `.gitattributes` | 保持 Shell/Python 脚本使用 LF 换行 |

文中 `C:\work\my-app`、`/srv/my-app`、`/tmp/my-app-release` 均为通用示例，请替换为自己的路径。真实服务器地址、账号、密码和密钥应通过本地配置或已有 Git 认证提供，不应写入仓库。

## 1. Windows Git Push 与自动打包

先下载或 Clone 本工具包：

```powershell
git clone https://github.com/Grtwwh2019/git-deploy-toolkit.git
```

将本工具包的脚本复制到**业务应用 Git Clone 的根目录**，并合并 `.gitignore` 和 `.gitattributes`。脚本会切换到自身所在目录运行，因此应在业务仓库内执行。业务仓库需要已有 Remote 和远程 `main` 分支；首次建库时先完成普通的初始 Commit 和 Push。

使用 Git Credential Manager、SSH Agent 等已有 Git 认证方式。确认 `origin` 指向业务应用仓库：

```powershell
Set-Location C:\work\my-app
git remote -v
git switch main
git add .
git commit -m "Update application"
.\push_and_package.ps1 -Remote origin -Branch main -OutputDir packages
```

脚本会检查当前分支和干净工作区，Fetch 远程分支，记录远程 Base 与本地 Target，确认可快进后 Push，再校验远程 SHA，最后打包。默认输出：

```text
packages/deployment_<base前8位>_to_<target前8位>.zip
```

普通 `git push`、SourceTree 的 Push 按钮和网页提交不会触发此脚本。没有新 Commit 时不生成 ZIP；分支分叉时应先自行 Merge/Rebase。Push 成功后如果打包失败，Push 不会撤销，可按下一节手动重建包。

## 2. 增量打包

在业务仓库根目录运行，替换两个示例 SHA 为真实 Commit。Base 必须是 Target 的祖先：

```powershell
$Base = 'BASE_COMMIT_SHA'
$Target = 'TARGET_COMMIT_SHA'
.\package.ps1 -Base $Base -Target $Target -OutputDir packages
```

`package.ps1` 固定先 Fetch `origin main`。需要离线打包或使用其他 Remote/分支时，在两个 Commit 对象已存在本地的情况下直接运行 Python：

```powershell
python .\package.py $Base $Target --output-dir packages
```

包中包含：

- `manifest.json`：完整 Base/Target SHA、变更路径、旧/新 SHA-256 和 Git 文件模式。
- `files/`：新增、修改文件的目标内容。
- `deleted_files.txt`：删除路径清单，供人工检查；部署以 Manifest 为准。
- `deploy.sh`、`rollback.sh`、`deploy_engine.py`：Linux 执行脚本。

包含两个版本之间的所有净变化；重命名表现为删除加新增。已跟踪且发生变化的数据文件也会被打包；不会按扩展名自动排除。仅支持普通 Git 文件（`100644`、`100755`）；符号链接、子模块及特殊条目会被拒绝。生成的 ZIP 包含业务代码，传输前应检查是否含已提交的配置或密钥。

## 3. Linux 首次部署状态

目标目录必须已经存在。首次部署前，独立核对目标目录中的 Git 管理文件与 Base Commit 一致，包括内容和文件模式。只在确认一致后初始化版本状态：

```bash
TARGET_DIR=/srv/my-app
BASE_COMMIT=REPLACE_WITH_VERIFIED_FULL_BASE_SHA
mkdir -p "$TARGET_DIR/.deployment-state"
printf '%s\n' "$BASE_COMMIT" > "$TARGET_DIR/.deployment-state/current_commit.txt"
```

不要仅为绕过校验而改写状态。后续部署会自动维护该文件，无需重复初始化。状态不匹配、旧文件内容异常或新增文件覆盖已有本地文件时，部署会停止。

## 4. Linux 部署与 Dry Run

通过自己的安全传输方式将 ZIP 放到 Linux。在**业务目标目录之外的新目录**中解压，保留该目录以供回滚。以下 `deployment_BASE_to_TARGET.zip` 为文件名占位符：

```bash
unzip deployment_BASE_to_TARGET.zip -d /tmp/my-app-release
cd /tmp/my-app-release
bash deploy.sh /srv/my-app --dry-run
```

Dry Run 会校验部署版本、旧文件内容和新文件校验和，并列出操作；不会修改目标文件、版本状态或创建备份。核对输出后，停止可能修改相关文件的应用或任务，再执行：

```bash
bash deploy.sh /srv/my-app
```

实际部署先检查全部受影响文件，再在发布目录的 `backup/` 下保存原文件和日志；应用新增/修改/删除后更新版本状态。仅删除 Manifest 中列明的文件，保留无关的未跟踪运行数据。完成后按业务应用自己的方法验证并恢复运行；本工具不会重启服务。

## 5. Rollback

使用**原发布目录及其 `backup/`**，停止可能修改相关文件的任务，然后先预演、再执行：

```bash
cd /tmp/my-app-release
bash rollback.sh /srv/my-app --dry-run
bash rollback.sh /srv/my-app
```

回滚会恢复备份文件、删除此次部署新增的文件，并将版本状态退回 Base Commit。受影响文件在部署后被修改时，校验通常会拒绝覆盖；请先调查冲突。保留的备份可供审计，重新部署应重新解压到一个新目录。

## 边界与故障处理

- 增量包不是首次全量安装程序；目标必须与 Base Commit 对应。
- 每次只允许一个部署或回滚过程；本工具没有进程锁或跨文件事务，不能替代完整发布平台。
- 单文件替换使用临时文件和原子替换；中途失败不会自动回滚。保留发布目录和日志，先运行回滚 Dry Run。若半完成状态触发冲突检查，应按日志及备份人工恢复，不能直接重复部署。
- 不处理数据库、外部系统、文件所有者、任意权限位、空目录布局或服务启停。文件权限按 Git 模式写为 `0644` / `0755`。
- 保持发布目录与目标目录分离，先在隔离副本上验证整个流程。
- `.gitignore` 不能清除已被 Git 跟踪的秘密。公开前检查源码、README、Git 历史及实际生成的业务包。

## 验证范围

发布前使用临时 Git 仓库和目标目录验证 Python 打包、Bash 入口、新增/修改/删除、可执行模式、Dry Run 不写入及 Rollback 恢复；并检查版本冲突和载荷校验失败。Windows PowerShell 脚本已做代码检查，尚未在 Windows 环境实测。
