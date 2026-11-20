# Git Deploy Toolkit

适用于手动 ETL 发布：开发人员在 Linux 开发仓库提交代码，Windows 获取并选择性 Cherry-pick 到 GitLab 生产分支的本地分支；运行 `publish.ps1` 推送 GitLab `main`，自动生成增量部署 ZIP。将 ZIP 传到生产 Linux，解压后仅运行 Shell 即可部署、预演或回滚。

```text
Linux 开发 Git 仓库 ──fetch──> Windows 本地生产分支
                                  │ 手动 cherry-pick 指定 Commit
                                  │ publish.ps1
                                  ├──push──> GitLab main
                                  └──ZIP───> 生产 Linux → deploy.sh
```

**部署基线：生产服务器已经部署到本次 Push 前的 GitLab `main`。** ZIP 对比的是该 Base Commit 与 Cherry-pick 后实际推送的 Target Commit，包含全部净新增/修改/删除，不会把未选中的开发分支改动带入生产。重命名作为删除与新增处理。

## 环境：这些命令为什么需要？

Windows 需要 Git、Python 3.9+（`python` 命令）和 PowerShell 5.1+。Python 只负责本地读取 Git 对象及生成 ZIP，无额外 Python 依赖。

生产 Linux **不需要 Python、Git、tar 或 base64**。运行依赖如下：

| 命令 | 用途 |
| --- | --- |
| Bash 3.2+ | 执行部署与回滚 Shell |
| `cp`、`mv`、`rm` | 备份、替换和删除文件 |
| `mkdir`、`rmdir` | 创建目录、取得和释放部署锁 |
| `chmod`、`stat` | 设置或保留文件权限 |
| `mktemp` | 在同一文件系统内创建替换用的临时文件 |
| `sha256sum`、`shasum` 或 `openssl`，任选一个 | 校验旧文件、新文件与备份内容，防止误覆盖或文件损坏 |
| `unzip`，可选 | 在服务器解压 ZIP；也可以在 Windows 解压后传输整个发布目录 |

不知道服务器是否具备这些命令时，先单独复制 `check-env.sh` 到服务器并运行：

```bash
bash check-env.sh
```

它只检查环境，不修改生产文件。部署/回滚入口也会执行同样的检查；缺少命令时在替换文件之前退出。`unzip` 属于解压阶段，不是解压后执行 Shell 的依赖。

## 1. Windows 准备与 Cherry-pick

建议将工具包保存在业务仓库之外，例如 `C:\tools\git-deploy-toolkit`；业务仓库位于 `C:\work\my-app`。不需要把工具脚本复制、提交到业务 Git 仓库。

业务仓库的 `origin` 指向 GitLab，另一个 Remote（以下称为 `etl`）指向 Linux 开发 Git 仓库。服务器连接地址使用自己的本地 Git 配置、SSH 或 Git Credential Manager，工具不保存账号或密码。

```powershell
Set-Location C:\work\my-app

# 仅首次配置时需要；将占位符替换为自己的 Linux Git SSH 地址。
git remote add etl 'SSH_URL_OF_LINUX_ETL_REPOSITORY'

# 发布前先对齐 GitLab 生产分支，再获取开发提交。
git fetch origin main
git switch main
git merge --ff-only origin/main
git fetch etl

# dev 是示例开发分支名，可自行替换。
git log --oneline etl/dev
git cherry-pick SELECTED_COMMIT_SHA_1 SELECTED_COMMIT_SHA_2
```

如果发生 Cherry-pick 冲突，先人工解决并完成 `git cherry-pick --continue`。`publish.ps1` 不替你选择开发 Commit，也不自动处理冲突。

## 2. 一次命令：Push main 并生成 ZIP

Cherry-pick 完成、工作区干净后运行：

```powershell
& C:\tools\git-deploy-toolkit\publish.ps1 -RepoPath C:\work\my-app
```

默认 Remote 为 `origin`、远程生产分支为 `main`、输出目录为工具包下的 `packages`。也可以显式指定：

```powershell
& C:\tools\git-deploy-toolkit\publish.ps1 `
  -RepoPath C:\work\my-app `
  -Remote origin `
  -Branch main `
  -OutputDir C:\releases\my-app
```

当前本地分支可以叫 `main` 或自己的生产工作分支；脚本始终将捕获的当前 HEAD 推送到指定远程的 `main`。请先确认当前分支就是准备发布的生产历史，并检查 `git remote -v`。

脚本按以下顺序执行：

1. 检查工作区、当前分支和 Remote，Fetch 并捕获远程 `main` 的 Base SHA 与本地 Target SHA。
2. 确认 Base 是 Target 的祖先，并在 Push 前检查所有增量文件能否打包。
3. 保存包含 Base/Target 的本地恢复记录，随后推送捕获的 Target。
4. 通过租约检查拒绝其他人同时改变远程分支。使用 `--force-with-lease` 仅作条件更新，前面的祖先检查保证此次更新为快进。
5. 再次检查远程 SHA 等于 Target，然后生成 ZIP。

输出示例：

```text
packages/
  deployment_<base前12位>_to_<target前12位>.zip
  publish_<base前12位>_to_<target前12位>.json
```

恢复记录保存在本地，包含 Commit、Remote 别名、分支和发布状态，不包含密码或服务器地址。不要把业务部署包或恢复记录提交到公开仓库；若自定义输出到业务仓库内部，应在业务仓库的 `.gitignore` 中排除该目录。

**普通 `git push`、SourceTree Push 按钮不会自动生成 ZIP。** 请使用 `publish.ps1`；旧的 `push_and_package.ps1` 也会转调它。远程已等于本地 HEAD 时不推送、不生成包。若两个 Commit 没有净文件变化，打包预检查会停止，不产生空部署包。

## 3. ZIP 内有什么？

```text
deployment_...zip
  deploy.sh              # 部署入口
  rollback.sh            # 回滚入口
  deploy_common.sh       # Shell 实现
  check-env.sh           # 环境检查，可单独运行
  manifest.tsv           # Shell 读取的变更清单（纯数据，不作为命令执行）
  manifest.json          # 同一清单的可读 JSON 版本
  deleted_files.txt      # 删除列表，供人工审核
  RELEASE.txt            # 此包的 Commit 和操作命令
  files/                 # 新增及修改文件的最终内容，保留仓库相对路径
```

文件内容直接读取 Git Commit 对象，不受 Windows 工作区 CRLF 转换或未提交文件影响。所有已跟踪且发生变化的普通文件都会进入增量，包括数据文件和配置文件；请在发布前审核清单，确认没有秘密或不应发布的数据。SHA-256 校验用于完整性和冲突检测，不代替可信的包传输渠道。

变更的符号链接、子模块、特殊 Git 文件、包含控制字符/反斜杠/冒号的路径以及 `.git`、`.deployment-state` 路径会被拒绝。文件与目录相互转换需拆成独立发布。相同名称的 ZIP 不会被覆盖，重建时请选择新的输出目录。

## 4. 生产 Linux：解压后只执行 Shell

以下 `/srv/my-app` 为通用生产根目录示例。仓库 `jobs/a.sh` 将对应 `/srv/my-app/jobs/a.sh`，所有增删改都采用相同的相对路径映射。

将 ZIP 放到生产目标目录之外，并解压到一个**全新发布目录**：

```bash
unzip deployment_BASE_to_TARGET.zip -d /tmp/my-app-release
cd /tmp/my-app-release
bash check-env.sh
bash deploy.sh /srv/my-app --dry-run
```

若服务器没有 `unzip`，可在 Windows 解压，再以二进制传输方式复制整个发布目录。保持 Shell 文件为 LF 换行。无需额外执行 Python。

Dry Run 校验版本、全部受影响的旧文件和载荷，列出 `A`/`M`/`D` 操作；不写入生产文件、版本、锁或备份。核对无误并停止可能修改这些文件的 ETL 任务后执行：

```bash
bash deploy.sh /srv/my-app
```

实际部署会再次校验，取得目录锁，**先备份全部受影响的旧文件，再开始替换**。只删除清单中明确列出的文件，不扫描删除运行目录，不影响无关的未跟踪数据。单文件采用同目录临时文件加原子替换；修改文件通常保留原生产权限，Git 可执行模式发生变化时采用新 Git 模式；新增文件使用 `0644` / `0755`。

首次部署若没有版本文件，会按已确认的前提假定服务器等于 Base，并校验每个受影响文件；不需要手工初始化。成功后将 Target 写入：

```text
<生产根目录>/.deployment-state/current_commit.txt
```

后续发布必须与 Base 匹配。此记录不是全目录审计，未变化文件仍由你保证与生产分支一致。部署完成后按业务方法验证应用，再恢复 ETL 任务；工具不会重启服务。

## 5. 回滚

保留原发布目录及其自动生成的 `backup/`。停止相关 ETL 任务后，在**原发布目录**运行：

```bash
bash rollback.sh /srv/my-app --dry-run
bash rollback.sh /srv/my-app
```

回滚先校验所有所需备份与生产内容，然后恢复修改/删除的原文件、删除本次新增文件，并恢复之前的版本记录；若首次部署之前没有版本记录，则回滚后仍不保留该记录。原文件权限也会恢复。

部署成功后若受影响文件又被修改，回滚会拒绝覆盖，需先调查。回滚绑定原目标目录和原清单，不能拿另一个包或目录的备份使用。备份保留供检查，不能在同一发布目录重复部署；请重新解压到新目录。

## 6. 失败与重建

### Push 成功、打包失败

Push 不会被撤销。根据 `publish_*.json` 中捕获的 Base/Target 手动重建；不能把已更新的远程 HEAD 当成旧 Base：

```powershell
& C:\tools\git-deploy-toolkit\package.ps1 `
  -RepoPath C:\work\my-app `
  -Base BASE_FULL_COMMIT_SHA `
  -Target TARGET_FULL_COMMIT_SHA `
  -OutputDir C:\releases\my-app-rebuild
```

两个 Commit 对象已存在本地时，该命令不 Fetch、不 Push，可以离线重建。也可在业务仓库根目录直接执行：

```powershell
python C:\tools\git-deploy-toolkit\package.py BASE_SHA TARGET_SHA --output-dir C:\releases\my-app-rebuild
```

推送报错但结果不明时，恢复记录状态为 `push_failed_or_unknown`。先核对远程历史，不要直接重新发布。远程在 Push 后再次被别人更新时也会停止生成包，需核对生产实际版本。

### 部署中途失败

不会自动跨文件回滚。保留发布目录和备份，先执行回滚 Dry Run。日志会在文件操作前记录意图；正常可恢复的部分部署允许文件仍为旧内容或已是新内容。若出现其他内容或备份损坏，会拒绝继续，需人工调查。

正常退出和信号中断会释放锁；强制终止或断电可能留下 `<生产根目录>/.deployment-state/lock`。确认没有其他部署/回滚进程后，才可人工用 `rmdir` 删除这个空锁目录，再执行回滚。不要删除 `backup/`。

本工具不处理数据库、外部系统、文件所有者、空目录布局或服务启停。工具锁仅防止本工具并发执行，不能阻止其他 ETL 进程改写文件；发布期间仍需停止相关任务。

## 源码与测试

`publish.ps1` 是新发布入口；`package.ps1`/`package.py` 负责重建；Linux 执行代码全部在四个 `.sh` 文件中。旧版 `deploy_engine.py` 已移除。

```bash
python3 -B -m unittest discover -s tests -v
```

测试使用临时应用仓库、模拟 GitLab 裸仓库和生产目录。覆盖选择性 Cherry-pick、Push/打包、并发拒绝、打包失败后的恢复，以及增删改/重命名、Unicode/二进制内容、权限、无 Python 的 Shell 部署、Dry Run、中断回滚、错误版本、校验失败和符号链接拒绝。PowerShell 集成测试在发现 `pwsh` 时执行，也可用 `TOOLKIT_PWSH` 指定可执行文件。Windows PowerShell 5.1 与实际生产 Linux 环境仍需在你的环境中验收。
