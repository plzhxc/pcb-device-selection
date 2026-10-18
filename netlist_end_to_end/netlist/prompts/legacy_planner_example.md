{
  "plan": {
    "complexity": "simple",
    "crossPageNet": {
      "inputs": [
        "VCC3V3",
        "GND",
        "I2C1_SCL"
      ],
      "outputs": []
    },
    "design_rules": [
      "不得虚构 supplierPart"
    ],
    "main_chip": {
      "designator": "U1",
      "device_name": "INPUT_CONFIRMED_MODEL",
      "device_type": "chip",
      "note": "型号来自输入；完整 pin map 待数据手册核验。",
      "pins": {
        "1": "VCC3V3",
        "2": "GND",
        "3": "I2C1_SCL"
      },
      "supplier_part": "",
      "value": ""
    },
    "notes": "示例中的 pin 只能在真实输入提供时使用。",
    "sub_tasks": [
      {
        "acceptance": [
          "电容两端分别连接 VCC3V3 和 GND"
        ],
        "context": "VCC3V3 到 GND 的局部去耦；取值来源见 guidance。",
        "expected_devices": "1 颗去耦电容",
        "id": "power_decoupling",
        "needed_nets": [
          "VCC3V3",
          "GND"
        ],
        "role": "power_decoupling"
      }
    ]
  }
}