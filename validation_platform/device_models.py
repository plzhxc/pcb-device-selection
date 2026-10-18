"""Preset device models for large platform parts.

Some board-level anchors are not normal LCSC C-number devices.  For example a
SoC or LPDDR4 chip can have hundreds of pins and an extremely long datasheet.
Passing that raw context to the netlist agent is noisy and often causes
invented ports.  This registry stores compact, reusable design guidance that
can be injected as bounded local context without an external vector store.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from typing import Any, Dict, Optional


def _norm_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


DEVICE_MODELS: Dict[str, Dict[str, Any]] = {
    "RK3566": {
        "model_id": "RK3566",
        "device_family": "rockchip_application_processor",
        "kind": "large_soc",
        "aliases": ["RK3566", "ROCKCHIPRK3566", "RK3566SOC"],
        "context_policy": {
            "mode": "rag_summary",
            "avoid_full_datasheet": True,
            "preferred_sources": [
                "fixture.interface_contract",
                "refer_netlist_guidance",
                "pcbagent_cache",
                "device_model",
            ],
        },
        "datasheet_cache": {
            "PCBAgent_connection_guide": (
                "RK3566 is a large application processor. Do not enumerate every unused BGA ball. "
                "Generate a controlled SoC core: power rails, reset, boot strap, crystal/clock, DDR, eMMC, "
                "RGMII, USB, HDMI/MIPI/CSI/DSI and GPIO header nets only when required by fixture neighbors "
                "or reference topology. Unused IC pins must be explicit NC instead of being exported as "
                "crossPageNet. DDR nets must connect to the LPDDR4 module with the same LPDDR4_* net names."
            ),
            "PCBAgent_pin_description": (
                "Important RK3566 pin groups: DDR controller pins use LPDDR4_CA/CLK/CKE/CS/ODT/RESET/DQ/DQS/DM "
                "families; eMMC uses EMMC_CLK/CMD/D0-D7/RSTN/DS; Ethernet MAC uses RGMII_TXD0-3, "
                "RGMII_RXD0-3, RGMII_TXCLK/RXCLK/TXCTL/RXCTL and MDIO/MDC; USB uses USB*_DP/DM and SS TX/RX "
                "pairs; audio uses I2S_MCLK/BCLK/LRCK/SDI/SDO; display/camera use MIPI/CSI/DSI named pairs."
            ),
            "PCBAgent_electrical_summary": (
                "Keep rails separated by function: VCC_DDR/VCC0V6_DDR for DDR, VCC_1V8 for 1.8V IO, "
                "VCC3V3_SYS/VCC_3V3 for 3.3V IO, VDD_LOGIC/VDD_CPU/VDD_GPU for core domains. Every rail needs "
                "local decoupling near the SoC. RESET/BOOT/clock pins cannot float. High-speed differential "
                "pairs must stay as paired nets."
            ),
            "PCBAgent_custom_requirements": (
                "For large SoC modules, quality is judged by interface topology, not by inventing all pins. "
                "Use fixture interface_contract and refer_netlist_guidance as the authority for exposed nets. "
                "Do not export generic GPIO/PAD/NC-like nets unless the fixture names that external interface."
            ),
        },
        "topology_hints": [
            "超大 SoC 采用模型化生成：只生成与 fixture 邻居/参考网表相关的接口组，不枚举所有未用 BGA ball。",
            "RK3566 的 LPDDR4 连接必须使用 LPDDR4_* 命名族，并与 LPDDR4 模块一一闭合；未用 IC pin 写 NC，不能主动暴露为 crossPageNet。",
            "电源/复位/晶振/boot strap 是 SoC 最小系统硬约束；外设接口只在需求或邻居模块存在时生成。",
        ],
        "interface_groups": {
            "ddr_lpddr4": [
                "LPDDR4_CLKP_A",
                "LPDDR4_CLKN_A",
                "LPDDR4_CLKP_B",
                "LPDDR4_CLKN_B",
                "LPDDR4_CKE0_A",
                "LPDDR4_CKE1_A",
                "LPDDR4_CKE0_B",
                "LPDDR4_CKE1_B",
                "LPDDR4_CS0N_A",
                "LPDDR4_CS1N_A",
                "LPDDR4_CS0N_B",
                "LPDDR4_CS1N_B",
                "LPDDR4_RESETN",
                "LPDDR4_A0_A",
                "LPDDR4_A1_A",
                "LPDDR4_A2_A",
                "LPDDR4_A3_A",
                "LPDDR4_A4_A",
                "LPDDR4_A5_A",
                "LPDDR4_A0_B",
                "LPDDR4_A1_B",
                "LPDDR4_A2_B",
                "LPDDR4_A3_B",
                "LPDDR4_A4_B",
                "LPDDR4_A5_B",
                "LPDDR4_DQ[0..15]_A",
                "LPDDR4_DQ[0..15]_B",
                "LPDDR4_DQS[0..1]P_A",
                "LPDDR4_DQS[0..1]N_A",
                "LPDDR4_DQS[0..1]P_B",
                "LPDDR4_DQS[0..1]N_B",
                "LPDDR4_DM[0..1]_A",
                "LPDDR4_DM[0..1]_B",
            ],
            "emmc": ["EMMC_CLK", "EMMC_CMD", "EMMC_D0..D7", "EMMC_RSTN", "EMMC_DS"],
            "rgmii": ["RGMII_TXD0..3", "RGMII_RXD0..3", "RGMII_TXCLK", "RGMII_RXCLK", "RGMII_TXCTL", "RGMII_RXCTL", "MDIO", "MDC"],
            "usb": ["USB*_DP", "USB*_DM", "USB3*_TXP/TXN", "USB3*_RXP/RXN"],
        },
    },
    "LPDDR4": {
        "model_id": "LPDDR4",
        "device_family": "lpddr4_sdram",
        "kind": "large_memory",
        "aliases": ["LPDDR4", "LPDDR4SDRAM", "LPDDR4MEMORY", "C2842431", "K4F6E3S4HM", "K4F6E3S4HMMGCJ"],
        "context_policy": {
            "mode": "rag_summary",
            "avoid_full_datasheet": True,
            "preferred_sources": [
                "fixture.interface_contract",
                "refer_netlist_guidance",
                "pcbagent_cache",
                "device_model",
            ],
        },
        "datasheet_cache": {
            "PCBAgent_connection_guide": (
                "LPDDR4 should be generated as a memory interface block paired to the SoC DDR controller. "
                "Use the same LPDDR4_* net names as the SoC/reference. Do not create random GPIO-style nets. "
                "Command/address, clock, control and data byte-lane nets must close to the SoC through top_port. "
                "Power pins need grouped decoupling; ZQ/RZQ and VREF/VREFCA/VREFDQ handling must be explicit."
            ),
            "PCBAgent_pin_description": (
                "LPDDR4 key groups: CA/A0-A5 for each channel, CKP/CKN differential clocks, CKE0/1, CS0N/1N, "
                "ODT0/1_CA, RESETN, DQ0-DQ15 per channel, DQS0/1P/N, DM0/1, VDD/VDDQ/VDD2/VSS, VREF and ZQ/RZQ."
            ),
            "PCBAgent_electrical_summary": (
                "LPDDR4 is sensitive to rail separation and termination. Keep VDD/VDDQ/VDD2 rails named from "
                "fixture/reference, add decoupling from each rail group to GND, and do not leave ZQ/RZQ or VREF "
                "floating unless the exact reference marks it NC."
            ),
            "PCBAgent_custom_requirements": (
                "Treat LPDDR4 as a template-guided high-speed memory interface. Prefer reference topology/RAG "
                "for pin groups. Missing unused balls may be NC only for the IC; passive decoupling and RZQ/VREF "
                "parts must be connected."
            ),
        },
        "topology_hints": [
            "LPDDR4 模块不要由 LLM 自由发明端口；DDR 总线必须用 LPDDR4_* 信号族与 RK3566 DDR 控制器闭合。",
            "LPDDR4 的 VDD/VDDQ/VDD2/VREF/ZQ/RZQ 不是普通 system_IO，必须连接到电源/地/参考/电阻网络或参考网表指定网络。",
            "只允许 IC 未用 ball 写 NC；DDR 去耦电容、RZQ 电阻、VREF 网络这类非 IC 器件必须真实连通。",
        ],
        "interface_groups": {
            "ca_control": [
                "LPDDR4_A0_A..A5_A",
                "LPDDR4_A0_B..A5_B",
                "LPDDR4_CLKP_A/B",
                "LPDDR4_CLKN_A/B",
                "LPDDR4_CKE0/1_A/B",
                "LPDDR4_CS0N/1N_A/B",
                "LPDDR4_ODT0/1_CA_A/B",
                "LPDDR4_RESETN",
            ],
            "data": [
                "LPDDR4_DQ0..15_A",
                "LPDDR4_DQ0..15_B",
                "LPDDR4_DQS0/1P_A/B",
                "LPDDR4_DQS0/1N_A/B",
                "LPDDR4_DM0/1_A/B",
            ],
            "power_reference": ["VDD", "VDDQ", "VDD2", "VSS", "VREFCA", "VREFDQ", "ZQ/RZQ"],
        },
    },
    "ESP32S3WROOM": {
        "model_id": "ESP32S3WROOM",
        "device_family": "espressif_wifi_ble_module",
        "kind": "complex_module",
        "aliases": [
            "ESP32-S3-WROOM",
            "ESP32-S3-WROOM-1",
            "ESP32-S3-WROOM-1-N16R8",
            "ESP32S3WROOM",
            "ESP32S3WROOM1N16R8",
        ],
        "context_policy": {
            "mode": "rag_summary",
            "avoid_full_datasheet": True,
            "preferred_sources": [
                "fixture.interface_contract",
                "database_parameters",
                "device_model",
            ],
        },
        "datasheet_cache": {
            "PCBAgent_connection_guide": (
                "ESP32-S3-WROOM module generation should focus on stable board-level peripherals: 3V3 power "
                "decoupling, EN reset RC/pull-up, GPIO0 boot strap/download button, UART0/USB programming nets, "
                "I2C pull-ups, SPI display/SD nets, I2S audio nets, DVP camera nets and RF antenna/feed only when "
                "the fixture exposes them. Do not enumerate unused GPIO pads as crossPageNet; unused module pins "
                "may be NC because the module is an IC/module."
            ),
            "PCBAgent_pin_description": (
                "Key ESP32-S3-WROOM groups: 3V3/GND supply, EN reset, GPIO0 boot, UART0 TXD/RXD, USB D+/D-, "
                "SPI CLK/MOSI/MISO/CS/DC/RST/BL for LCD/SD, I2C SCL/SDA, I2S MCLK/BCLK/LRCK/DIN/DOUT, camera "
                "PCLK/VSYNC/HREF/XCLK/D0-D7, and selected GPIO interrupts or chip selects from fixture contracts."
            ),
            "PCBAgent_electrical_summary": (
                "Use 3.3V supply with local 100nF and bulk decoupling. EN should not float: use pull-up and reset "
                "capacitor/button. GPIO0 should have a defined pull-up and optional download button to GND. I2C "
                "SCL/SDA require pull-ups to 3V3. Signal series resistors may be used for SPI/USB/RF where specified."
            ),
            "PCBAgent_custom_requirements": (
                "Only expose nets named by fixture interface_contract, neighbors or known subsystem interfaces. "
                "For unconnected ESP32 GPIO pins, write NC instead of creating extra crossPageNet ports. Passive "
                "parts must be locally connected and cannot be NC or same-net."
            ),
        },
        "topology_hints": [
            "ESP32-S3-WROOM 只生成板级需要的接口组；未用 GPIO 写 NC，不主动暴露成 crossPageNet。",
            "EN、GPIO0、I2C pull-up、USB/UART 下载、SPI LCD/SD、I2S 音频、DVP 摄像头是稳定外围约束。",
            "RF/天线相关网络如果不是 fixture 明确边界，必须保持模块内闭合或删除残缺支路。",
        ],
        "interface_groups": {
            "power_reset_boot": ["VCC3V3", "GND", "EN", "GPIO0", "RESETN"],
            "programming": ["USB_DP", "USB_DM", "UART0_TXD", "UART0_RXD", "DTR", "RTS"],
            "i2c": ["I2C_SCL", "I2C_SDA"],
            "display_spi": ["LCD_SPI_CLK", "LCD_SPI_MOSI", "LCD_SPI_CS", "LCD_DC", "LCD_RST", "LCD_BL"],
            "audio_i2s": ["I2S_MCLK", "I2S_BCLK", "I2S_LRCK", "I2S_DIN", "I2S_DOUT"],
            "camera_dvp": ["CAM_XCLK", "CAM_PCLK", "CAM_VSYNC", "CAM_HREF", "CAM_D0..D7"],
        },
    },
    "RK809": {
        "model_id": "RK809",
        "device_family": "rockchip_pmic_codec",
        "kind": "complex_pmic",
        "physical_supplier_part": "C2939628",
        "aliases": ["RK809", "RK809-5", "RK809PMIC", "RK8095"],
        "context_policy": {
            "mode": "rag_summary",
            "avoid_full_datasheet": True,
            "preferred_sources": [
                "fixture.interface_contract",
                "database_parameters",
                "device_model",
            ],
        },
        "datasheet_cache": {
            "PCBAgent_connection_guide": (
                "RK809/RK809-5 is a PMIC/CODEC. Generate regulator rails as named outputs only when they are "
                "required by the fixture or neighbor SoC: BUCK/LDO output pins need output capacitors to GND; "
                "switching bucks need inductor and bootstrap/input/output capacitors; I2C pins connect to SoC "
                "SCL/SDA with pull-ups if the bus owner does not provide them; reset/interrupt/32k clock pins "
                "must not float. Audio codec pins are generated only if audio subsystem neighbors require them."
            ),
            "PCBAgent_pin_description": (
                "Important RK809 groups: VIN/VSYS input, GND/PGND, BUCK switch/output/feedback pins, LDO outputs, "
                "I2C SCL/SDA, INT/RESET/PWRON, RTC/32K clock, battery/charger sense where used, and audio codec "
                "ADC/DAC/headphone/speaker pins if enabled by the fixture."
            ),
            "PCBAgent_electrical_summary": (
                "Each regulator output requires local decoupling. Buck outputs require inductor from switch node "
                "to rail and output capacitors from rail to GND. Feedback and enable pins need deterministic "
                "connections from datasheet/cache or fixture. Do not expose unused regulator/audio pins as ports."
            ),
            "PCBAgent_custom_requirements": (
                "For PMIC modules, the stable output is a rail-level topology: named power rails, required passives, "
                "I2C/control pins and no invented crossPageNet. Unused IC pins may be NC; non-IC passives must be "
                "fully connected."
            ),
        },
        "topology_hints": [
            "RK809/RK809-5 按电源轨组建模：BUCK/LDO 输出、反馈/使能、I2C 控制、RTC/32K、复位/中断。",
            "只暴露 fixture/邻居需要的电源轨和控制信号；未使用 IC pin 写 NC，不生成多余端口。",
            "BUCK/LDO 外围是稳定结构：输入/输出电容、电感、反馈/补偿/自举按 datasheet/cache/RAG 确认。",
        ],
        "interface_groups": {
            "power_in": ["VCC_5V0", "VSYS", "VIN"],
            "rails": ["VDD_CPU", "VDD_GPU", "VDD_LOGIC", "VCC_DDR", "VCC_1V8", "VCC3V3_SYS"],
            "control": ["I2C_SCL", "I2C_SDA", "PMIC_INT", "PMIC_RESET", "PWRON"],
            "clock_rtc": ["RTC_32K", "CLK_32K"],
        },
    },
}


_ALIASES: Dict[str, str] = {}
for _model_key, _model in DEVICE_MODELS.items():
    _ALIASES[_norm_key(_model_key)] = _model_key
    for _alias in _model.get("aliases") or []:
        _ALIASES[_norm_key(_alias)] = _model_key


def get_device_model(*keys: Any) -> Optional[Dict[str, Any]]:
    """Return a copy of a matched preset device model."""
    for key in keys:
        norm = _norm_key(key)
        if not norm:
            continue
        model_key = _ALIASES.get(norm)
        if model_key:
            return copy.deepcopy(DEVICE_MODELS[model_key])
        for alias, candidate in _ALIASES.items():
            if len(alias) >= 6 and (alias in norm or norm in alias):
                return copy.deepcopy(DEVICE_MODELS[candidate])
    return None


def merge_device_model_cache(model: Optional[Dict[str, Any]], base_cache: Dict[str, str]) -> Dict[str, str]:
    """Merge model-provided cache sections with regular datasheet cache."""
    merged = dict(base_cache or {})
    if not isinstance(model, dict):
        return merged
    model_cache = model.get("datasheet_cache") or {}
    if not isinstance(model_cache, dict):
        return merged
    for key, value in model_cache.items():
        if value and not merged.get(key):
            merged[str(key)] = str(value)
    return merged


def model_topology_hints(model: Optional[Dict[str, Any]]) -> list[str]:
    if not isinstance(model, dict):
        return []
    hints = model.get("topology_hints") or []
    if not isinstance(hints, Iterable) or isinstance(hints, (str, bytes)):
        return []
    return [str(item).strip() for item in hints if str(item or "").strip()]
