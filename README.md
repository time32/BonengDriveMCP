# BonengDriveMCP

面向 Boneng 驱动器的本地 MCP 服务，供 Codex 等 MCP 客户端调用。当前版本 **0.11.1**，提供 **54 个工具**。

通过已有 Drivesoft 通信组件连接真实驱动器，支持在线参数、运行控制、示波器及固件验证任务。固件代码修改由 Codex 的文件/终端工具完成，设备操作由本 MCP 执行。

## 当前支持的功能

| 功能 | 已实现内容 | 主要边界 |
| --- | --- | --- |
| 设备识别 | 串口只读握手、型号/固件识别、显式绑定设备 | CM55没有已验证唯一UID，换机须重新选择 |
| 网络发现 | 选择网卡、PROFINET DCP扫描、用户选择后TCP连接 | 当前网络适配为AN，尚无永久IP配置工具 |
| 在线参数目录 | 从当前设备读取分组、功能码地址、名称、位宽、权限及属性 | 各型号含义以在线目录为准，不依赖旧工程文件 |
| 参数读写 | 16/32位读取；单参数预览、一次写入、回读校验 | 每次读取最多20项；通用写参要求停机，命令寄存器受限制 |
| 参数备份 | 按组/全机读取实际配置、保存JSON快照、比较差异 | 批量配置仅能预览，尚无整套参数恢复/回滚 |
| 速度运行 | 上限按当前电机在线额定转速及设备限制确定；正反向、持续或限时运行 | 需明确现场条件；CM55暂限rpm；使用驱动器内部斜坡 |
| 运行中调速 | 同一连接中预览并执行调速，保持独立后台心跳；可给定零速 | 遵守本次会话上限/方向，给定回读不等于实际达速 |
| 相对/绝对定位 | 软件零点、指令脉冲坐标、位置JOG、内部加减速、到位后停机及恢复临时设置 | 当前只完成模拟/回环验证；不执行回零，不保持使能/转矩 |
| 停机与复位 | OFF1停机、核对运行位并释放PC控制；非运行状态下故障复位 | 软件停机接口，不能代替现场停机措施 |
| 示波器 | 从在线目录选择1～4个16位P通道，立即触发，导出原始/换算CSV | 2～1000ms偶数周期，最多20000点且不超过40秒；无可靠丢点检测 |
| 固件升级 | 固件登记封存、SHA-256、型号/版本预检、单次烧录、Flash读回、重启后验证 | AN串口/TCP，CM55串口；已适配的SPI格式，手动重启 |
| 验证任务 | 可选备份→烧录一次→等待手动重启→核对身份→采集→统计/对比 | 任务本身不启动电机，不自动重刷或判定根因 |
| 离线分析 | CSV范围/均值/RMS/峰峰值、参数集比较、批量变更预览 | 离线枚举/程序集参考工具需要自行提取的可选参考数据库 |

完整工具名称见[工具索引](docs/TOOLS.md)，运动细节见[运动控制](docs/MOTION_CONTROL.md)。

## 支持设备与实测范围

| 产品 | 连接 | 当前适配与实测 |
| --- | --- | --- |
| AN PN | 串口或TCP 1000 | 平台9、已核对的9.10构建（6908/6A22）；已有部分读写、采集、300rpm控制及升级闭环记录 |
| CM55 EC | Windows串口，460800/8N1 | 平台3、6.12/6629；在线目录/读参/备份、300rpm限时运行，以及500/1000/2000rpm运行、1000→2000rpm在线调速和停止有短时实机记录 |

CM55本次在线调速后，稳定阶段采样反馈约为 **1992～2008rpm**；最终确认零速、停止状态及PC控制权释放。运行中仍有**风扇相关警告50/1**，故障码为0。该记录不代表警告原因已解决、额定3000rpm已验收或长期运行已验证。

**位置控制仍待实机验收。** CM55实际参数写入、故障复位、示波器采集和烧录也需要分别验收。其他型号、固件构建和硬件配置需额外适配。

开发环境 **161项单元/厂商组件回环测试通过，0跳过**；干净环境STDIO检查确认54个工具。缺少厂商软件或未构建桥接器时，对应测试会明确跳过。详见[验证范围](docs/VALIDATION.md)。

## 环境要求

- Windows；实机通信依赖已检查的 **Drivesoft V1.28测试版**及依赖文件，需自行准备。
- Python **3.11+**，安装示例使用Python 3.12；PowerShell **7**；.NET Framework及其C#编译器。
- 使用DCP扫描时需要可用网卡和WinPcap/Npcap；仅串口操作不需要抓包驱动。
- 桥接构建脚本核对原厂EXE哈希；其他Drivesoft版本需核对后适配。

这是源码发布包，不含原厂EXE/DLL/许可证/数据库、驱动固件、固件源码、协议文档、设备绑定、现场波形和日志，也不是免安装的独立EXE。

## 安装：按顺序执行

以下在 **PowerShell 7** 中运行。目录可替换，中文和空格路径均可使用；先进入项目目录，再运行脚本。

```powershell
# 1. 获取源码并进入目录；已有源码可跳过clone
git clone https://github.com/time32/BonengDriveMCP.git C:\Work\BonengDriveMCP
Set-Location 'C:\Work\BonengDriveMCP'

# 2. 明确使用Python 3.12创建虚拟环境
py -3.12 --version
py -3.12 -m venv .venv

# 3. 改成你自己的原厂软件目录
$env:BONENG_RELEASE_DIR = 'C:\Vendor\BonengDrivesoft 100(V1.28)_Test'
$env:BONENG_PWSH_PATH = (Get-Command pwsh -ErrorAction Stop).Source

# 4. 安装依赖、生成本机配置、构建通信桥
.\scripts\Setup.ps1
.\scripts\Build-ReuseBridge.ps1 -ReleaseDir $env:BONENG_RELEASE_DIR
```

没有`py -3.12`时，安装对应Python，或使用自己的Python 3.11+解释器绝对路径执行`-m venv .venv`。不要借用其他电脑的`.venv`，也不要在Python 3.9/3.10下创建环境。

构建成功后应有：

```text
.venv/Scripts/python.exe
codex.config.toml
workspace/reuse_bridge/ReuseReadProbe.exe
workspace/reuse_bridge/ReuseDeviceBridge.exe
workspace/reuse_bridge/ReuseFirmwareUpgrade.exe
```

源目录和`scripts/`须保留；无需手动打开原上位机窗口，也不要让其他软件同时占用该串口。

## 接入 Codex

### 合并生成的配置

1. 打开项目根目录自动生成的`codex.config.toml`。
2. 将`[mcp_servers.boneng_drivesoft]`和对应`.env`表合并到Codex实际使用的配置文件。默认用户级路径为`%USERPROFILE%\.codex\config.toml`；也可使用受信任项目内的`.codex/config.toml`。保留已有其他配置，同名服务器应更新原表，避免重复定义。
3. 保存为UTF-8，在Codex的MCP服务器设置中重启该连接，再开始新聊天或刷新工具列表。
4. 如安装了Codex CLI，运行`codex mcp list`检查注册状态。

用户级配置适合在多个驱动固件源码项目中共用。连接参数依据[OpenAI官方MCP文档](https://learn.chatgpt.com/docs/extend/mcp)。

下面说明配置结构，实际应使用安装脚本按本机位置生成的文件：

```toml
[mcp_servers.boneng_drivesoft]
command = "C:/Work/BonengDriveMCP/.venv/Scripts/python.exe"
args = ["-u", "C:/Work/BonengDriveMCP/run_server.py"]
cwd = "C:/Work/BonengDriveMCP"
enabled = true
startup_timeout_sec = 30
tool_timeout_sec = 180

[mcp_servers.boneng_drivesoft.env]
BONENG_RELEASE_DIR = "C:/Vendor/BonengDrivesoft 100(V1.28)_Test"
BONENG_WORKSPACE = "C:/Work/BonengDriveMCP/workspace"
BONENG_DATA_DIR = "C:/Work/BonengDriveMCP/data"
BONENG_PWSH_PATH = "C:/Program Files/PowerShell/7/pwsh.exe"
```

PowerShell安装在其他位置时，以`Get-Command pwsh`显示的路径为准。TOML路径可使用`/`；双引号内使用反斜杠时应写成`\\`。原AN串口还需要用户核对的`BONENG_SERIAL_MAC`，当前旧适配限COM7；CM55不需要AN的MAC。

### 验证连接

先向Codex发送：

> 调用 boneng_drivesoft 的 get_capabilities，确认版本0.11.1和已配置能力。本次只读，不启动电机、不写参、不烧录。

客户端应列出54个工具。仅看到服务器名称不代表设备已连接，`get_capabilities`不会主动连接驱动器。

CM55首次使用：

> 设备是COM7上的CM55。先只读识别型号和固件，确认后绑定该串口并读取状态。

AN网络设备：

> 列出网卡并扫描设备，将结果给我选择，选定后再建立连接。

实际端口和目标由使用者指定，发布包没有预选实机设备。

## 使用示例

| 需求 | 可以给Codex的指令 |
| --- | --- |
| 读参数 | “读取当前设备的B05.01和D00.05，说明在线名称与单位。” |
| 备份 | “备份当前设备的B05组参数，并与上次备份比较。” |
| 速度运行 | “电机空载、现场可立即停机，正向500rpm持续运行，上限按在线额定转速；先核对设备状态。” |
| 在线调速 | “将当前目标改为1000rpm，保持同一心跳连接。” |
| 停止 | “停机，确认实际零速并释放PC控制。” |
| 位置 | “先询问是否将当前停止位置设为软件零点，单位用指令脉冲，再预览相对移动10000脉冲。” |
| 波形 | “列出当前可采通道，由我选择后以2ms、1000点采集。” |
| 固件闭环 | “登记我编译好的固件，按本次授权预检烧录；我重启后自动采集已选通道。” |

持续运行由独立后台进程维持心跳，**关闭聊天不会停止电机**。未说明运行方式时会询问持续或限时。调速遵守会话上限和方向；若另设较低上限，或新速度超出会话固定编码范围，需要停机后重建会话。

大范围给定采用百分比编码，预览显示实际分辨率及量化值。例如基准3000rpm时，可能使用0.3rpm步长，不能将所有量程都视作0.01rpm精度。

绝对定位需要明确软件/设备零点；10000指令脉冲不代表一圈。MCP不自动执行回零或推断机械零点；断电、复位、重新回零或重启MCP后须重建参考。

## 当前限制

- 单实例仅一个后台运动任务，尚不能并行控制五台驱动器或实现多轴实时同步。
- 位置控制仍待实机验收；阈值触发、32位示波器、批量参数执行尚未接入。
- 示波器时间轴为名义采样周期，无设备时间戳及可靠丢点检测。
- 自动验证任务不替代人工重启；编译和代码修改由Codex协调，不会仅凭统计结果宣称已定位根因。
- 单台设备上的短时结果不能推广为其他配置或长期运行验收。

## 测试与源码包

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\smoke_test_clean.py
.\.venv\Scripts\python.exe scripts\package_github.py
```

`smoke_test_clean.py`使用空数据和隔离目录，不连接真实设备。厂商组件回环使用本机模拟端点，缺少厂商软件时会跳过对应测试。

仓库的[release-assets目录](https://github.com/time32/BonengDriveMCP/tree/main/release-assets)提供当前源码ZIP及SHA-256校验文件；修改后，打包脚本在本机`dist/`生成新包。ZIP只包含发布源码、脚本、测试、文档和文件哈希清单。

更多文档：[部署细节](docs/GETTING_STARTED.md) · [运动控制](docs/MOTION_CONTROL.md) · [工具索引](docs/TOOLS.md) · [验证范围](docs/VALIDATION.md) · [发布边界](docs/DISTRIBUTION.md)。

## 许可与需要补充的信息

本项目采用[MIT许可证](LICENSE)，版权署名为Copyright (c) 2026 time32。厂商软件和第三方依赖分别遵循自身许可，见[许可与第三方声明](LICENSE_NOTICE.md)。

后续完善发布还可补充：原厂依赖的获取说明，以及优先补齐的型号/固件和位置控制实机验收范围。这些不影响按本文部署当前功能。
