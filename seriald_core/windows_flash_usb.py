"""Read present PnP identifiers when the flashing executor finds no Fastboot interface."""
import ctypes
import re


def empty_scan_reason():
    api = ctypes.WinDLL('cfgmgr32')
    count = ctypes.c_ulong()
    flags = 0x101  # CM_GETIDLIST_FILTER_ENUMERATOR | CM_GETIDLIST_FILTER_PRESENT
    unknown = '刷机执行器未发现 Fastboot 接口。请退出占用设备的刷机工具，在设备管理器核对下载设备的状态。'
    for _ in range(2):
        if api.CM_Get_Device_ID_List_SizeW(ctypes.byref(count), 'USB', flags) or not 0 < count.value <= 1_000_000:
            return unknown
        buf = ctypes.create_unicode_buffer(count.value)
        status = api.CM_Get_Device_ID_ListW('USB', buf, count, flags)
        if status == 0x1a:  # USB topology changed between the size and list calls.
            continue
        if status:
            return unknown
        devices = []
        for item in buf[:].split('\0'):
            match = re.match(r'USB\\(VID_361C&PID_[0-9A-F]{4}|VID_18D1&PID_4E11)(?:&|\\)', item, re.I)
            if match:
                devices.append(match[1].upper())
        if devices:
            return ('Windows 已枚举 USB 设备（' + '、'.join(sorted(set(devices))) +
                    '），刷机执行器未发现 Fastboot 接口。请确认下载模式，关闭 TitanFlasher 并核对该接口的驱动。')
        return 'Windows 当前未枚举到匹配的 USB 下载设备。请在连接刷机线的控制机上检查下载模式和 USB 数据线。'
    return unknown
