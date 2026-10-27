# MCP工具索引

版本0.11.1，共54个工具。此表按服务注册生成；读取设备仍需选择正确目标，接口存在不代表所有型号均已实机验收。

| 工具 | 类型 | 用途 |
| --- | --- | --- |
| `get_capabilities` | 只读 | 查询离线能力和可选只读探测配置；不会主动连接设备 |
| `probe_device_read_only` | 只读 | 复用原软件读取参数版本、A02.00 软件版本、A00.00 状态及 C05.19～21 实际 MAC；460800/8N1，结束后关闭端口，不写参数 |
| `inspect_serial_device` | 只读设备/网络 | 新串口产品的只读检查，支持 CM55 平台3及 AN 平台9，460800/8N1 |
| `select_serial_device` |  | 绑定用户指定的 CM55 EC 6.12 串口：只读核对平台/软件/板号，保存本地选择，旧计划失效 |
| `list_network_interfaces` | 只读 | 列出可用于原扫描器同版 DCP 发现库的本机活动网卡、IPv4 和掩码；不会扫描或连接设备 |
| `scan_network_devices` | 只读设备/网络 | 在指定本机网卡执行 DCP Identify，只列出设备 IP/MAC/名称，不修改网络配置、不选择或连接设备；超时 2～15 秒 |
| `select_network_device` | 本地预览/保存 | 用户明确选定扫描列表中的设备后，保存其 IP/MAC/网卡；仅更新本地选择，不建立 TCP 连接 |
| `get_network_selection` | 只读 | 查看当前保存的网络设备选择；选择状态不表示设备在线 |
| `probe_selected_device_read_only` | 只读设备/网络 | 对用户已选择的设备，通过原程序 TCP 1000 接口读版本、状态及 MAC，核对扫描 MAC 后返回；每次结束断开，不写参数 |
| `register_firmware_build` | 本地预览/保存 | 登记编译产物：AN PN 9.10 用默认 profile/软件910；CM55 EC 6.12 用 cm55_ec_spi_612/软件612 |
| `get_firmware_build` | 只读 | 读取已登记编译产物并核对封存文件哈希，不联系设备 |
| `prepare_firmware_validation` | 本地预览/保存 | 离线预览一轮验证任务，绑定已登记产物、用户已选设备、问题/假设、采集通道/周期/点数、备份与重启等待时限 |
| `start_firmware_validation` | 设备操作 | 用户授权本轮预览后启动独立后台任务：预检并只烧录一次，等待手动重启，验证正确设备/固件后自动采集并生成报告 |
| `get_firmware_validation_status` | 只读 | 查询验证阶段、手动重启等待、错误及完整采集/统计报告，不联系设备 |
| `cancel_firmware_validation` | 本地预览/保存 | 取消验证后续步骤，不强杀正在写 Flash 的升级器；若已开始采集可等本次结束，不重复烧录 |
| `preflight_firmware_upgrade` | 本地预览/保存 | 只读预检生成 15 分钟一次性烧录计划，核对原升级器、MAC、平台、停止状态及运行位 |
| `start_firmware_upgrade` | 设备操作 | 仅在用户明确要求本次烧录后执行预检计划，实际擦写主控 Flash并返回 job_id |
| `get_firmware_upgrade_status` | 只读 | 读取烧录进度、Flash 读回校验和启动验证结果；不联系设备 |
| `verify_firmware_upgrade_boot` | 只读 | 用户完成手动断电重启后，只读核对已成功烧录任务的设备身份、版本和升级状态复位；不会发送重启或烧录命令 |
| `discover_device_parameter_catalog` | 只读设备/网络 | 只读刷新设备功能码目录：A/A1 身份与参数版本，B6 大组、B4 子组数量、B5 实际地址/参数数量/组名，再用 B1/B3 获取属性/名称 |
| `search_device_parameters` | 只读设备/网络 | 搜索选中设备在线获取的功能码名称和属性，支持分组及分页 |
| `read_device_parameters` | 只读设备/网络 | 按设备在线目录读取 1～20 个功能码的实时属性、上下限、名称和值，支持16/32位首字地址 |
| `create_device_parameter_snapshot` |  | 只读设备实际配置并保存新的 JSON 备份：默认全部可读可写的非预留、非 P 参数；groups 可限定组，include_monitoring=true 才包含只读监控项 |
| `list_device_parameter_snapshots` | 只读 | 列出本地实机参数备份的身份、范围、时间和数量，不联系设备 |
| `get_device_parameter_snapshot` | 只读 | 分页查看已有实机备份的定义、原始值和精确工程值；这是备份时读数，不是当前值 |
| `compare_device_parameter_snapshots` | 只读 | 离线比较两份实机备份，分别报告数值变化、定义变化、新增和缺失；标明设备、版本及采集范围是否一致 |
| `prepare_device_parameter_write` | 本地预览/保存 | 只读预览一个参数的修改，value 为显示单位下的精确十进制字符串，生成 10 分钟有效的一次性计划；包含旧值、目标值和掉电保存影响，不实际写设备 |
| `apply_device_parameter_write` | 设备操作 | 用户明确指定本次参数和值后执行计划：重查设备/固件/旧值/属性，停机状态下写入单个参数并读回 |
| `list_oscilloscope_channels` | 只读设备/网络 | 只读核对设备身份/参数版本，分页列出设备在线目录中的可采 16 位 P 通道；支持名称/功能码搜索或 P00～P15 筛选，不依赖 AN380.bnproj，不启动采集 |
| `capture_oscilloscope` | 设备操作 | 用户选定 1～4 个 P 通道后执行 E1/E2 连续示波器采集，立即触发 |
| `read_drive_runtime_status` | 只读设备/网络 | 只读状态机、控制字、状态字、PC 指令和速度/电流相关参数；不会取得控制权或启动电机 |
| `plan_drive_run` | 本地预览/保存 | 预览速度运行，不启动 |
| `read_drive_motion_limits` | 只读设备/网络 | 只读当前激活电机数据组、在线额定/最大转速、频率和速度基准 |
| `prepare_drive_speed_change` | 本地预览/保存 | 为当前后台速度会话预览调速，读取会话版本，不修改速度 |
| `apply_drive_speed_change` | 设备操作 | 执行用户要求的单次调速，复用运行连接及心跳，优先于长采集；再次核对版本和电机参数并回读给定 |
| `configure_drive_position_reference` | 本地预览/保存 | 停机读取反馈后保存本次MCP会话的坐标参考，不写驱动器、不执行回零 |
| `read_drive_position_status` | 只读设备/网络 | 只读位置反馈及坐标参考有效性；运行中复用心跳连接 |
| `plan_drive_position_move` | 本地预览/保存 | 预览相对/绝对位置运动 |
| `execute_drive_position_move` | 设备操作 | 用户允许预览中的位移及临时配置后执行一次后台定位 |
| `execute_drive_run` | 设备操作 | 仅在用户明确允许该计划实际运动时调用 |
| `get_drive_run_status` | 只读 | 读取持续运行任务的心跳记录、状态字、实际速度反馈、停止结果和进程状态；不另开设备连接 |
| `stop_drive_pc_control` | 设备操作 | 停止 PC 控制并释放控制权；仅软件命令，不是安全急停，也不保证机械已停止 |
| `reset_drive_fault` | 设备操作 | 用户要求故障复位时，核对 MAC 和非运行状态后发送 PC 故障复位位的上升沿，再读回确认故障位清除 |
| `list_parameter_catalogs` | 只读 | 列出发布包提取的参数目录；目录版本不代表测试设备型号已经匹配 |
| `search_parameters` | 只读 | 按中文名称、功能码或说明搜索参考参数，分页返回原始字段及来源 |
| `get_parameter_definition` | 只读 | 读取指定功能码的参考定义；出厂值不是实时值，通讯地址尚未验证 |
| `lookup_enum_value` | 只读 | 在参数设定范围中查找显式枚举说明；返回参考含义，不判断实际故障原因 |
| `create_parameter_set` | 本地预览/保存 | 把用户提供的值保存为 MCP 离线 JSON 参数集；保留已有文件，不写入设备 |
| `list_parameter_sets` | 只读 | 列出 MCP 工作目录中的离线参数集 |
| `get_parameter_set` | 只读 | 读取一个离线参数集；不代表设备实时参数 |
| `compare_parameter_sets` | 只读 | 比较同一参数目录的两个离线参数集，列出新增、删除、修改 |
| `plan_batch_configuration` | 只读 | 预览批量修改及待验证项；文件中删除参数表示保留设备原值，当前不能执行 |
| `summarize_waveform` | 只读 | 统计工作目录内已有 UTF-8 CSV 波形的范围、均值、RMS；时间列必须为秒 |
| `get_release_interfaces` | 只读 | 查询主程序的静态接口元数据；没有实例化或调用原软件通信方法 |
