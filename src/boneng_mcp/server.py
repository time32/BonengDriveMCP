"""MCP stdio server with offline tools, read-only probes, and vendor firmware jobs."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import StrictBool, StrictFloat, StrictInt, StrictStr

from . import __version__
from .catalog import Catalog
from .parameter_sets import ParameterSets
from .original_probe import OriginalSoftwareProbe
from .network import NetworkDevices
from .firmware import FirmwareUpgrades
from .device import DeviceOperations
from .snapshots import ParameterSnapshots
from .validation import FirmwareValidation
from .serial_inspection import SerialInspection
from .paths import Settings, UserInputError, read_json
from .waveforms import summarize_waveform as summarize_csv


INSTRUCTIONS = (
    "Boneng adapter provides offline reference/file tools and bounded live operations "
    "that reuse the inspected original Drivesoft application. Standalone connections close after operations; continuous-run "
    "reads/metadata/scope and reviewed speed changes share the worker connection with heartbeat/stop priority. Generic parameter writes require stopping. "
    "Motion ceilings use the live active motor rating and user limits. Continuous speed running is supported with duration_s=null; "
    "ask continuous versus timed only if user intent is missing, and reuse confirmed session conditions. No external ramp is generated. "
    "Online speed changes need prepare/apply previews within the run's authorized ceiling/directions. Position moves need explicit "
    "coordinate zero; ask whether the current stopped position should be this session's software zero. "
    "Use command pulses by default (for example 10000 pulses); never assume 10000 means pulses per revolution. "
    "No limit-switch homing is performed. Re-establish the reference after power-cycle, reset or rehoming. "
    "Position moves stop/release after arrival and do not maintain holding torque. "
    "Network workflow: list interfaces, scan on one interface, present the results to the user, "
    "then select only the device the user explicitly chose. Never auto-select based on device family or prior serial MAC. "
    "Scanning and selecting do not open TCP; probe_selected_device_read_only checks the selected MAC on TCP port 1000. "
    "Catalog defaults are not live readings. Raw 通讯地址 may be a GROUP BASE, not a parameter register. "
    "Use discover_device_parameter_catalog/search_device_parameters for the connected device: B6/B4/B5 enumerate its "
    "actual groups and addresses, B1/B3 define its parameters. Device identity/parameter revision is checked before cache use. "
    "Offline search_parameters/get_parameter_definition/lookup_enum_value remain reference-only and must not supply "
    "another model's semantics. Live parameter tools never substitute saved project values. Preview writes and execute only "
    "the user's specified parameter and value. Motion requires the user's concrete motor/load state, direction, maximum speed, "
    "duration and local stop availability; never invent them or run a motor merely to test an interface. Channel listing uses "
    "the selected device's online catalog: ask the user to select channels before capture; capture clears previous oscilloscope data. Motion, writes and "
    "capture must be reported with their actual bench-validation limits. Never claim an unimplemented operation succeeded. Firmware upgrades support the "
    "inspected legacy AN image or an explicitly registered AN PN 9.10 / CM55 EC 6.12 SPI build with its own digest/expected release. AN uses pinned serial MAC or selected TCP; CM55 uses its explicitly selected serial binding. "
    "Preflight makes a reviewable one-use plan and does not flash. Start only for the user's explicit request for a new flash; "
    "a previously completed flash is not authorization to repeat it. Poll the job; only flash_verified confirms Flash readback. "
    "The user must power-cycle manually before verify_firmware_upgrade_boot. Call get_capabilities first; "
    "select catalog IDs via list_parameter_catalogs. Batch previews do not validate device readiness. "
    "Firmware-validation tasks persist independently, flash once, wait for manual reboot, verify identity and collect. "
    "They never start a motor; completed means evidence ready for assistant source-based analysis, not a proven root cause. "
    "For a new serial product use inspect_serial_device for read-only discovery. For user-chosen CM55 EC 6.12 use select_serial_device, "
    "then existing device tools with transport=serial. CM55 has no verified unique UID; port/board/platform fingerprint cannot distinguish identical replacement hardware. "
    "Reselect after replacing serial hardware; selection invalidates old plans. CM55 supports rpm control, not Hz; source/simulation success is not physical motion/flash acceptance. "
    "CSV time values must be seconds. Offline parameter sets contain supplied values; device snapshots contain acquired values."
)
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
NETWORK_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
DEVICE_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_environment()
    catalog = Catalog(settings.data_dir / "catalog.sqlite3")
    parameter_sets = ParameterSets(settings.workspace_dir, catalog)
    probe = OriginalSoftwareProbe(settings.release_dir)
    network = NetworkDevices(settings.workspace_dir, probe)
    firmware = FirmwareUpgrades(settings.release_dir, settings.workspace_dir, settings.data_dir, network)
    device = DeviceOperations(settings.release_dir, settings.workspace_dir, settings.data_dir, network, firmware)
    snapshots = ParameterSnapshots(settings.workspace_dir, device)
    validation = FirmwareValidation(settings, firmware, device, snapshots)
    serial_inspection = SerialInspection(settings.release_dir, settings.workspace_dir, firmware)
    server = FastMCP("boneng-drivesoft", instructions=INSTRUCTIONS, log_level="WARNING")

    @server.tool(annotations=READ)
    def get_capabilities() -> dict[str, Any]:
        """查询离线能力和可选只读探测配置；不会主动连接设备。"""
        datasets = catalog.list_catalogs() if catalog.path.is_file() else []
        probe_info = probe.describe()
        network_info = network.describe()
        firmware_info = firmware.describe()
        device_info = device.describe()
        available = ["reference_parameter_lookup", "offline_parameter_sets", "configuration_diff", "batch_preview",
                     "existing_csv_summary", "assembly_interface_inventory", "registered_firmware_builds", "firmware_validation_preview_and_status"]
        if probe_info["configured"]:
            available.append("fixed_read_only_device_probe")
        if network_info["configured"]:
            available += ["network_discovery", "explicit_network_device_selection"]
            if probe_info["configured"]:
                available.append("selected_device_tcp_read_only_probe")
        if firmware_info["configured"]:
            available += ["pinned_firmware_preflight", "pinned_firmware_upgrade", "firmware_job_status", "firmware_boot_verification", "firmware_validation_sequence"]
        if device_info["configured"]:
            available += ["cm55_serial_read_only_inspection", "explicit_cm55_serial_selection", "cm55_serial_parameter_scope_rpm_adapters", "online_device_parameter_catalog", "online_device_parameter_search", "live_parameter_read", "live_parameter_write_preview_and_readback", "online_oscilloscope_channels",
                          "bounded_oscilloscope_capture", "run_connection_scope_and_reads", "device_parameter_snapshots", "device_snapshot_comparison", "runtime_status", "bounded_run_control",
                          "live_motor_rated_speed_limits", "continuous_speed_control", "online_speed_change", "position_coordinate_reference", "relative_and_absolute_position_moves"]
        return {
            "server_version": __version__, "mode": "offline_with_device_tools" if firmware_info["configured"] else ("offline_with_read_only_probe" if probe_info["configured"] else "offline"), "device_connected": False,
            "catalogs": [{"id": row["id"], "parameter_count": row["row_count"], "source": row["source"],
                          "extraction_warnings": row["extraction_warnings"], "ambiguous_codes": row["ambiguous_codes"]} for row in datasets],
            "available": available,
            "read_only_probe": probe_info,
            "network": network_info,
            "firmware": firmware_info,
            "device_operations": device_info,
            "pending_protocol_and_bench_validation": ["other_parameter_write_bench", "batch_apply", "comprehensive_fault_diagnosis", "commissioning_wizard", "other_speed_build_load_duration_physical_bench", "relative_and_absolute_position_physical_bench", "registered_firmware_physical_bench", "post_reboot_validation_physical_bench", "other_platform_firmware_upgrade"],
            "workspace_dir": str(settings.workspace_dir), "catalog_reference_only": True,
        }

    @server.tool(annotations=READ)
    def probe_device_read_only(port: str = "COM7") -> dict[str, Any]:
        """复用原软件读取参数版本、A02.00 软件版本、A00.00 状态及 C05.19～21 实际 MAC；460800/8N1，结束后关闭端口，不写参数。"""
        return probe.run(port)

    @server.tool(annotations=NETWORK_READ)
    def inspect_serial_device(mode: Literal["identity", "catalog", "read"] = "identity", port: str = "COM7",
                              codes: list[str] | None = None, group: str = "", query: str = "",
                              limit: StrictInt = 50, offset: StrictInt = 0) -> dict[str, Any]:
        """新串口产品的只读检查，支持 CM55 平台3及 AN 平台9，460800/8N1。identity 只读通用 A/A1 握手；catalog 按设备 B6/B4/B5/B1/B3 读取并分页搜索实际定义；read 先重新获取目录，再按实际地址读取1～20个功能码。不会沿用 AN 的 MAC/状态地址，不验证产品物理唯一身份，不使用旧缓存；不能写参数、采集、运行或烧录。"""
        return serial_inspection.inspect(mode, port, codes, group, query, limit, offset)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def select_serial_device(port: str = "COM7", profile: Literal["cm55_ec_612", "an_pn_910"] = "cm55_ec_612") -> dict[str, Any]:
        """绑定用户指定的 CM55 EC 6.12 串口：只读核对平台/软件/板号，保存本地选择，旧计划失效。现有参数、备份、采集及 rpm 运行工具使用 transport='serial'。CM55 无唯一UID，换机须重选。an_pn_910 可在核对原 COM7/已记录 AN MAC 后切回原串口配置。不写设备、不启动电机。"""
        return device.select_serial(port, profile)

    @server.tool(annotations=READ)
    def list_network_interfaces() -> dict[str, Any]:
        """列出可用于原扫描器同版 DCP 发现库的本机活动网卡、IPv4 和掩码；不会扫描或连接设备。"""
        return network.interfaces()

    @server.tool(annotations=NETWORK_READ)
    def scan_network_devices(interface_ip: str, timeout_s: StrictInt = 5) -> dict[str, Any]:
        """在指定本机网卡执行 DCP Identify，只列出设备 IP/MAC/名称，不修改网络配置、不选择或连接设备；超时 2～15 秒。"""
        return network.scan(interface_ip, timeout_s)

    @server.tool(annotations=LOCAL_WRITE)
    def select_network_device(scan_id: str, device_id: str) -> dict[str, Any]:
        """用户明确选定扫描列表中的设备后，保存其 IP/MAC/网卡；仅更新本地选择，不建立 TCP 连接。"""
        return network.select(scan_id, device_id)

    @server.tool(annotations=READ)
    def get_network_selection() -> dict[str, Any]:
        """查看当前保存的网络设备选择；选择状态不表示设备在线。"""
        return network.selection()

    @server.tool(annotations=NETWORK_READ)
    def probe_selected_device_read_only() -> dict[str, Any]:
        """对用户已选择的设备，通过原程序 TCP 1000 接口读版本、状态及 MAC，核对扫描 MAC 后返回；每次结束断开，不写参数。"""
        return network.probe_selected()

    @server.tool(annotations=LOCAL_WRITE)
    def register_firmware_build(firmware_path: str, label: str, expected_release_hex: str, expected_software_raw: StrictInt = 910,
                                expected_parameter_version_raw: StrictInt | None = None, source_revision: str = "",
                                profile: Literal["an_pn_spi_910", "cm55_ec_spi_612"] = "an_pn_spi_910") -> dict[str, Any]:
        """登记编译产物：AN PN 9.10 用默认 profile/软件910；CM55 EC 6.12 用 cm55_ec_spi_612/软件612。校验产品封装、封存并记录 SHA-256 和用户声明的发布号/参数版本，不烧录；封装不能证明应用型号兼容。CM55 仅串口升级。"""
        return firmware.builds.register(firmware_path, label, expected_release_hex, expected_software_raw, expected_parameter_version_raw, source_revision, profile)

    @server.tool(annotations=READ)
    def get_firmware_build(build_id: str) -> dict[str, Any]:
        """读取已登记编译产物并核对封存文件哈希，不联系设备。"""
        return firmware.builds.get(build_id)

    @server.tool(annotations=LOCAL_WRITE)
    def prepare_firmware_validation(build_id: str, problem: str, channels: list[str], sample_period_ms: StrictInt, points: StrictInt,
                                    transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7", wait_timeout_s: StrictInt = 600,
                                    backup_configuration: StrictBool = True, read_codes: list[str] | None = None,
                                    baseline_job_id: str = "", hypothesis: str = "") -> dict[str, Any]:
        """离线预览一轮验证任务，绑定已登记产物、用户已选设备、问题/假设、采集通道/周期/点数、备份与重启等待时限。此时不接触设备。启动后单次烧录、等待用户手动重启、核对版本并刷新目录，自动备份/采集/统计。不会启动电机、改运行模式或自动重启；统计不等于根因诊断。"""
        return validation.prepare(build_id, problem, channels, sample_period_ms, points, transport, port, wait_timeout_s, backup_configuration, read_codes, baseline_job_id, hypothesis)

    @server.tool(annotations=DEVICE_WRITE)
    def start_firmware_validation(plan_id: str) -> dict[str, Any]:
        """用户授权本轮预览后启动独立后台任务：预检并只烧录一次，等待手动重启，验证正确设备/固件后自动采集并生成报告。关闭 MCP 会话不终止任务；失败不会自动重刷，不控制电机。"""
        return validation.start(plan_id)

    @server.tool(annotations=READ)
    def get_firmware_validation_status(job_id: str = "") -> dict[str, Any]:
        """查询验证阶段、手动重启等待、错误及完整采集/统计报告，不联系设备。completed 表示数据准备好，仍须结合源码分析原因。"""
        return validation.status(job_id)

    @server.tool(annotations=LOCAL_WRITE)
    def cancel_firmware_validation(job_id: str) -> dict[str, Any]:
        """取消验证后续步骤，不强杀正在写 Flash 的升级器；若已开始采集可等本次结束，不重复烧录。"""
        return validation.cancel(job_id)

    @server.tool(annotations=LOCAL_WRITE)
    def preflight_firmware_upgrade(firmware_path: str, transport: Literal["serial", "selected_tcp"], port: str = "COM7", build_id: str = "") -> dict[str, Any]:
        """只读预检生成 15 分钟一次性烧录计划，核对原升级器、MAC、平台、停止状态及运行位。旧固件可直接给路径；新编译产物先 register_firmware_build，再给 build_id、firmware_path=""。不开始升级。"""
        return firmware.preflight(firmware_path, transport, port, build_id)

    @server.tool(annotations=DEVICE_WRITE)
    def start_firmware_upgrade(plan_id: str) -> dict[str, Any]:
        """仅在用户明确要求本次烧录后执行预检计划，实际擦写主控 Flash并返回 job_id。支持计划中的串口或选定 TCP 设备，复用原 F2/F1/F3；保持供电与连接，轮询结果，无自动重启。"""
        return firmware.start(plan_id)

    @server.tool(annotations=READ)
    def get_firmware_upgrade_status(job_id: str = "") -> dict[str, Any]:
        """读取烧录进度、Flash 读回校验和启动验证结果；不联系设备。不填 ID 时返回最近 MCP 任务，尚无任务时可查看此前独立脚本的实测记录，历史记录不代表新烧录。"""
        return firmware.status(job_id)

    @server.tool(annotations=READ)
    def verify_firmware_upgrade_boot(job_id: str) -> dict[str, Any]:
        """用户完成手动断电重启后，只读核对已成功烧录任务的设备身份、版本和升级状态复位；不会发送重启或烧录命令。"""
        return firmware.verify_boot(job_id)

    @server.tool(annotations=NETWORK_READ)
    def discover_device_parameter_catalog(transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7", group: str = "") -> dict[str, Any]:
        """只读刷新设备功能码目录：A/A1 身份与参数版本，B6 大组、B4 子组数量、B5 实际地址/参数数量/组名，再用 B1/B3 获取属性/名称。不依赖 bnproj。group 为空读取完整目录，也可指定 B05/P11 单组；返回摘要及本地完整目录路径，不读取全部当前值。身份引导当前支持 AN 平台 9，其他平台尚未适配。"""
        return device.discover_catalog(transport, port, group)

    @server.tool(annotations=NETWORK_READ)
    def search_device_parameters(query: str = "", group: str = "", limit: StrictInt = 50, offset: StrictInt = 0,
                                 transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        """搜索选中设备在线获取的功能码名称和属性，支持分组及分页。每次核对设备/参数版本；缺少目录时自动只读获取。返回字典身份、缓存时间和来源，不含当前值；当前值用 read_device_parameters。设备未提供枚举说明时不从其他型号补造。"""
        return device.search_device_parameters(query, group, limit, offset, transport, port)

    @server.tool(annotations=NETWORK_READ)
    def read_device_parameters(codes: list[str], transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        """按设备在线目录读取 1～20 个功能码的实时属性、上下限、名称和值，支持16/32位首字地址。AN核对MAC；CM55须先 select_serial_device，核对串口绑定/板号/平台；均核对参数版本/固件，不依赖导出工程。"""
        return device.read(codes, transport, port)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def create_device_parameter_snapshot(label: str, groups: list[str] | None = None, include_monitoring: StrictBool = False,
                                         transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        """只读设备实际配置并保存新的 JSON 备份：默认全部可读可写的非预留、非 P 参数；groups 可限定组，include_monitoring=true 才包含只读监控项。依据在线目录及实时 B1/C2，保存身份/版本、原始值、精确小数值和时间范围。逐项读取，非同一时刻快照；失败不发布完整备份，不写设备。当前格式不是 bnproj，尚不提供恢复下发。"""
        return snapshots.create(label, groups, include_monitoring, transport, port)

    @server.tool(annotations=READ)
    def list_device_parameter_snapshots(limit: StrictInt = 50) -> dict[str, Any]:
        """列出本地实机参数备份的身份、范围、时间和数量，不联系设备。"""
        return snapshots.list(limit)

    @server.tool(annotations=READ)
    def get_device_parameter_snapshot(snapshot_id: str, group: str = "", limit: StrictInt = 50, offset: StrictInt = 0) -> dict[str, Any]:
        """分页查看已有实机备份的定义、原始值和精确工程值；这是备份时读数，不是当前值。"""
        return snapshots.get(snapshot_id, group, limit, offset)

    @server.tool(annotations=READ)
    def compare_device_parameter_snapshots(baseline_id: str, target_id: str, limit: StrictInt = 50, offset: StrictInt = 0) -> dict[str, Any]:
        """离线比较两份实机备份，分别报告数值变化、定义变化、新增和缺失；标明设备、版本及采集范围是否一致。缺失项只报告，不恢复默认值，不写设备。"""
        return snapshots.compare(baseline_id, target_id, limit, offset)

    @server.tool(annotations=LOCAL_WRITE)
    def prepare_device_parameter_write(code: str, value: str, transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        """只读预览一个参数的修改，value 为显示单位下的精确十进制字符串，生成 10 分钟有效的一次性计划；包含旧值、目标值和掉电保存影响，不实际写设备。"""
        return device.prepare_write(code, value, transport, port)

    @server.tool(annotations=DEVICE_WRITE)
    def apply_device_parameter_write(plan_id: str) -> dict[str, Any]:
        """用户明确指定本次参数和值后执行计划：重查设备/固件/旧值/属性，停机状态下写入单个参数并读回。可能保存 EEPROM；失败不自动重试。运行/重启命令使用专用流程。"""
        return device.apply_write(plan_id)

    @server.tool(annotations=NETWORK_READ)
    def list_oscilloscope_channels(query: str = "", group: str = "", limit: StrictInt = 50, offset: StrictInt = 0,
                                   transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        """只读核对设备身份/参数版本，分页列出设备在线目录中的可采 16 位 P 通道；支持名称/功能码搜索或 P00～P15 筛选，不依赖 AN380.bnproj，不启动采集。采集前再核对实时属性。"""
        return device.channels(query, group, limit, offset, transport, port)

    @server.tool(annotations=DEVICE_WRITE)
    def capture_oscilloscope(codes: list[str], sample_period_ms: StrictInt, points: StrictInt, transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        """用户选定 1～4 个 P 通道后执行 E1/E2 连续示波器采集，立即触发。周期为 2ms 的整数倍，最多 40 秒/20000 点；会清除旧示波器缓冲区，结束发送停止并导出原始/换算 CSV。无设备时间戳，不能保证无丢点；不控制电机。"""
        return device.capture(codes, sample_period_ms, points, transport, port)

    @server.tool(annotations=NETWORK_READ)
    def read_drive_runtime_status(transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        """只读状态机、控制字、状态字、PC 指令和速度/电流相关参数；不会取得控制权或启动电机。"""
        return device.runtime(transport, port)

    @server.tool(annotations=LOCAL_WRITE)
    def plan_drive_run(speed: StrictFloat | StrictInt, maximum_abs_speed: StrictFloat | StrictInt | None = None, speed_unit: Literal["Hz", "rpm"] = "rpm", duration_s: StrictFloat | StrictInt | None = None,
                       motor_load_description: str = "", local_stop_available: StrictBool = False,
                       transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7", restore_control_mode: StrictInt | None = None, background: StrictBool = False, allow_direction_change: StrictBool = False) -> dict[str, Any]:
        """预览速度运行，不启动。默认上限取当前电机额定/最大转速较小值，maximum_abs_speed可降低。自动选择固定16位编码，超过327.67rpm可用百分比给定，预览给出实际量化速度和分辨率。duration_s默认null持续运行，用户未说明时先询问持续或限时；长于20秒自动后台执行，有限时长最多2147483.647秒。需电机/负载、现场停机条件及B05.01=0；不自动切换模式，不生成外部斜坡。仅用户明确允许运行中反向时设置allow_direction_change。"""
        return device.plan_run(speed, maximum_abs_speed, speed_unit, duration_s, motor_load_description, local_stop_available, transport, port, restore_control_mode, background, allow_direction_change)

    @server.tool(annotations=NETWORK_READ)
    def read_drive_motion_limits(transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        """只读当前激活电机数据组、在线额定/最大转速、频率和速度基准。适配器核对实时参数名称/格式；切换电机组或参数变化会使旧运动计划失效。"""
        return device.motion_limits(transport, port)

    @server.tool(annotations=LOCAL_WRITE)
    def prepare_drive_speed_change(speed: StrictFloat | StrictInt, job_id: str = "") -> dict[str, Any]:
        """为当前后台速度会话预览调速，读取会话版本，不修改速度。允许零速保持会话，遵守该会话已授权速度/方向；显示实际量化给定，沿用内部斜坡。"""
        return device.prepare_speed_change(speed, job_id)

    @server.tool(annotations=DEVICE_WRITE)
    def apply_drive_speed_change(plan_id: str) -> dict[str, Any]:
        """执行用户要求的单次调速，复用运行连接及心跳，优先于长采集；再次核对版本和电机参数并回读给定。给定回读成功不代表实际达速，失败不自动重试。"""
        return device.apply_speed_change(plan_id)

    @server.tool(annotations=LOCAL_WRITE)
    def configure_drive_position_reference(zero_method: Literal["current_stopped_position", "existing_device_zero"], minimum_position: StrictFloat | StrictInt | None = None,
            maximum_position: StrictFloat | StrictInt | None = None, reference_description: str = "", unit: Literal["pulses", "revolutions", "degrees"] = "pulses",
            pulses_per_revolution: StrictInt | None = None, transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        """停机读取反馈后保存本次MCP会话的坐标参考，不写驱动器、不执行回零。先询问是否把当前停止点设为软件零点；existing_device_zero使用用户已建立的设备坐标。默认指令脉冲；圈/角度须明确每圈指令脉冲数。可选同时提供最小/最大软件行程；省略时只防32位溢出，无机械限位。断电、复位、重新回零或MCP重启后须重建，不能声称已验证物理零点。"""
        return device.configure_position_reference(zero_method, minimum_position, maximum_position, reference_description, unit, pulses_per_revolution, transport, port)

    @server.tool(annotations=NETWORK_READ)
    def read_drive_position_status(transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        """只读位置反馈及坐标参考有效性；运行中复用心跳连接。未建立参考时仍可读原始指令脉冲反馈。"""
        return device.position_status(transport, port)

    @server.tool(annotations=LOCAL_WRITE)
    def plan_drive_position_move(position: StrictFloat | StrictInt, maximum_speed_rpm: StrictFloat | StrictInt, motor_load_description: str, local_stop_available: StrictBool,
            mode: Literal["relative", "absolute"] = "relative", unit: Literal["pulses", "revolutions", "degrees"] = "pulses", acceleration_ms: StrictInt = 500,
            position_tolerance_pulses: StrictInt = 1, timeout_s: StrictFloat | StrictInt | None = None, allow_mode_change: StrictBool = False,
            transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        """预览相对/绝对位置运动。先建立坐标及行程范围，速度为正整数rpm并受额定/在线JOG上限约束，加减速由驱动器处理。绝对目标按参考零点计算差值；列出临时模式及JOG参数变更，停机后恢复，可能写EEPROM。allow_mode_change须用户允许。默认等待定位完成后停止释放，可设超时且随时可停止；首版要求固定方向/电子齿轮选择。"""
        return device.plan_position_move(position, maximum_speed_rpm, motor_load_description, local_stop_available, mode, unit, acceleration_ms, position_tolerance_pulses, timeout_s, allow_mode_change, transport, port)

    @server.tool(annotations=DEVICE_WRITE)
    def execute_drive_position_move(plan_id: str) -> dict[str, Any]:
        """用户允许预览中的位移及临时配置后执行一次后台定位。重查坐标/反馈/配置并维持心跳，验证目标位置后OFF1停机释放并恢复设置；用现有运行状态和停止工具查询或中止。"""
        return device.execute_position_move(plan_id)

    @server.tool(annotations=DEVICE_WRITE)
    def execute_drive_run(plan_id: str) -> dict[str, Any]:
        """仅在用户明确允许该计划实际运动时调用。申请 PC 控制权、发送带单位的速度和心跳；持续模式返回后台 job_id，用 get_drive_run_status 查询、stop_drive_pc_control 停止。限时模式结束 OFF1 停机后释放；失败不自动重试。"""
        return device.run(plan_id)

    @server.tool(annotations=READ)
    def get_drive_run_status(job_id: str = "") -> dict[str, Any]:
        """读取持续运行任务的心跳记录、状态字、实际速度反馈、停止结果和进程状态；不另开设备连接。不填 ID 返回最近任务。缓存记录含时间，不能把旧记录当作当前运动状态。"""
        return device.run_status(job_id)

    @server.tool(annotations=DEVICE_WRITE)
    def stop_drive_pc_control(transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        """停止 PC 控制并释放控制权；仅软件命令，不是安全急停，也不保证机械已停止。"""
        return device.stop(transport, port)

    @server.tool(annotations=DEVICE_WRITE)
    def reset_drive_fault(transport: Literal["selected_tcp", "serial"] = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        """用户要求故障复位时，核对 MAC 和非运行状态后发送 PC 故障复位位的上升沿，再读回确认故障位清除。保持零给定，不申请控制权、不发送运行命令。故障条件未消除时失败，不自动重试。"""
        return device.reset_fault(transport, port)

    @server.tool(annotations=READ)
    def list_parameter_catalogs() -> list[dict[str, Any]]:
        """列出发布包提取的参数目录；目录版本不代表测试设备型号已经匹配。"""
        return catalog.list_catalogs()

    @server.tool(annotations=READ)
    def search_parameters(query: str, catalog_id: str = "", limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """按中文名称、功能码或说明搜索参考参数，分页返回原始字段及来源。"""
        return catalog.search(query, catalog_id, limit, offset)

    @server.tool(annotations=READ)
    def get_parameter_definition(catalog_id: str, code: str) -> dict[str, Any]:
        """读取指定功能码的参考定义；出厂值不是实时值，通讯地址尚未验证。"""
        return catalog.definition(catalog_id, code)

    @server.tool(annotations=READ)
    def lookup_enum_value(catalog_id: str, code: str, value: str) -> dict[str, Any]:
        """在参数设定范围中查找显式枚举说明；返回参考含义，不判断实际故障原因。"""
        definition = catalog.definition(catalog_id, code)
        description = definition["fields"].get("设定范围")
        if not isinstance(description, str):
            raise UserInputError("This parameter has no textual enumeration reference.")
        if not value.strip() or len(value) > 64:
            raise UserInputError("value must contain 1–64 characters.")
        matches = []
        for line in description.splitlines():
            match = re.match(r"^\s*([A-Za-z]?[0-9]+|0[xX][0-9A-Fa-f]+)\s*[:：]\s*(.+)$", line)
            if match and match.group(1).casefold() == value.strip().casefold():
                matches.append(match.group(2).strip())
        return {"catalog_id": catalog_id, "code": code, "name": definition["name"], "value": value, "matches": matches, "reference_only": True, "root_cause_diagnosed": False}

    @server.tool(annotations=LOCAL_WRITE)
    def create_parameter_set(set_id: str, catalog_id: str, values: dict[str, StrictStr | StrictInt | StrictFloat], note: str = "") -> dict[str, Any]:
        """把用户提供的值保存为 MCP 离线 JSON 参数集；保留已有文件，不写入设备。"""
        return parameter_sets.save(set_id, catalog_id, values, note)

    @server.tool(annotations=READ)
    def list_parameter_sets() -> list[dict[str, Any]]:
        """列出 MCP 工作目录中的离线参数集。"""
        return parameter_sets.list()

    @server.tool(annotations=READ)
    def get_parameter_set(set_id: str) -> dict[str, Any]:
        """读取一个离线参数集；不代表设备实时参数。"""
        return parameter_sets.load(set_id)

    @server.tool(annotations=READ)
    def compare_parameter_sets(baseline_id: str, target_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """比较同一参数目录的两个离线参数集，列出新增、删除、修改。"""
        return parameter_sets.compare(baseline_id, target_id, limit, offset)

    @server.tool(annotations=READ)
    def plan_batch_configuration(baseline_id: str, target_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """预览批量修改及待验证项；文件中删除参数表示保留设备原值，当前不能执行。"""
        return parameter_sets.plan(baseline_id, target_id, limit, offset)

    @server.tool(annotations=READ)
    def summarize_waveform(filename: str, time_column: str = "time_s") -> dict[str, Any]:
        """统计工作目录内已有 UTF-8 CSV 波形的范围、均值、RMS；时间列必须为秒。"""
        return summarize_csv(settings.workspace_dir, filename, time_column)

    @server.tool(annotations=READ)
    def get_release_interfaces(type_query: str = "communication") -> dict[str, Any]:
        """查询主程序的静态接口元数据；没有实例化或调用原软件通信方法。"""
        if not type_query.strip() or len(type_query) > 200:
            raise UserInputError("type_query must contain 1–200 characters.")
        inventory = read_json(settings.data_dir / "assembly_inventory.json")
        matched = [entry for entry in inventory["types"] if type_query.casefold() in entry["name"].casefold()]
        return {
            "evidence_kind": inventory["evidence_kind"], "application_sha256": inventory["sha256"],
            "device_calls_executed": False, "total_matching_types": len(matched), "types": matched[:10],
        }

    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
