{
  "cite_info": [],
  "guidance": "为每个电源引脚配置局部去耦，跨模块 I2C 网络保持同名。",
  "topology_rules": [
    {
      "reason": "输入未提供器件数据手册，作为待验证的通用约束。",
      "rule": "每个 VDD 引脚附近配置 100 nF 去耦",
      "source": "engineering_convention"
    }
  ]
}