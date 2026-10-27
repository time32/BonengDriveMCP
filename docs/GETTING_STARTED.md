# 部署与使用

## 1. 环境与安装

安装Python 3.11或更高版本、PowerShell 7及.NET Framework。先明确使用所选Python解释器执行`-m venv .venv`；完整安装顺序见仓库README。
在PowerShell 7中进入源码根目录，设置原软件路径后运行：

```powershell
$env:BONENG_RELEASE_DIR = 'C:\path\to\licensed\Drivesoft'
.\scripts\Setup.ps1
.\scripts\Build-ReuseBridge.ps1 -ReleaseDir $env:BONENG_RELEASE_DIR
```

桥接构建器会核对原软件哈希；只支持当前已检查的原程序。
`workspace/reuse_bridge/` 下生成的程序和依赖留在本机，不提交到GitHub。
网络扫描需WinPcap/Npcap；不使用网络扫描时不需要为串口操作安装抓包驱动。

## 2. 生成MCP配置

```powershell
.\.venv\Scripts\python.exe scripts\write_codex_config.py
```

生成的 `codex.config.toml` 包含实际绝对路径和180秒工具超时。
将该服务器表合并到 Codex 的用户配置，或受信任项目的 `.codex/config.toml`。
文件不存在时，也可运行 `scripts/install_project_config.py --apply`
注册当前项目；遇到已有不同配置时该脚本不会覆盖。
用户级配置可供不同固件源码项目中的聊天使用。

`codex.config.example.toml` 只展示字段，安装后应使用本机生成的版本。
服务由Codex通过STDIO启动，不需要手动打开原上位机窗口。
不要让原上位机同时占用相同串口。

## 3. 选择设备

- CM55：先只读检查，再在用户明确指定端口后 `select_serial_device`。
  平台/板号等只形成串口指纹，不能保证物理唯一身份，换设备后重新选择。
- AN网络：列网卡→DCP扫描→用户选择→保存选择；扫描不会自动选设备。
- 原AN串口：先核对设备MAC，在生成配置前设置 `BONENG_SERIAL_MAC`；
  该配置当前限已检查的COM7/AN协议。不要把演示MAC当成实机目标。

## 4. 数据与参考目录

在线参数工具不需要历史数据库。可选离线参考目录由用户自行提取：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-analysis.txt
.\.venv\Scripts\python.exe scripts\build_catalog.py
```

生成内容只保存在本机 `data/`。参考数据库中的保存值或默认值不是设备实时读数。
厂商程序集接口清单也是可选本地数据；缺失时相关工具会明确报告。

## 5. 运行与烧录

先读状态和模式，再预览和执行。CM55运行暂限rpm；工具不会自动切换控制模式。
给定使用驱动器内部斜坡，上限按在线电机额定/设备限制确定。持续运行是接口默认选项；有限时长可超过20秒，长任务自动后台执行，减速和通信可能增加停机完成时间。
持续运行使用独立后台进程，必须主动停止，不能靠关闭聊天停机。

AN固件用 `an_pn_spi_910`，CM55固件用 `cm55_ec_spi_612` 登记，
声明预期软件/发布号和可选参数版本。登记不证明程序内部型号兼容。
先预检，用户授权本次具体文件/设备后只执行一次；重启仍由现场手动完成。

## 环境变量

| 名称 | 作用 |
| --- | --- |
| BONENG_RELEASE_DIR | 自备原软件目录 |
| BONENG_DATA_DIR | 本地参考数据目录，默认项目data/ |
| BONENG_WORKSPACE | 本地任务/快照/波形目录，默认workspace/ |
| BONENG_SERIAL_MAC | 用户核对的AN串口目标，无默认设备 |
| BONENG_PWSH_PATH | PowerShell 7不在PATH时指定pwsh.exe |
