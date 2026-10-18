# 器件选型阶段 · 手工测试用例

从原始案例（`case_test/backup/cases/case*/input.txt`）里挑出来的，覆盖单芯片、
电源、混合信号、无线四类。每个用例给出**两个阶段**的输入与预期输出。

| 用例 | 原文点名器件 | 目录可确认 | 选型项 | 有推荐 | 未核对 |
| --- | --- | --- | --- | --- | --- |
| [case31-key-sound-toy](case31-key-sound-toy/input.txt) | 2 | 0 | 5 | 2 | 3 |
| [case30-pn532-nfc](case30-pn532-nfc/input.txt) | 7 | 4 | 7 | 4 | 3 |
| [case25-stm32g4-foc](case25-stm32g4-foc/input.txt) | 12 | 6 | 13 | 0 | 13 |
| [case29-lora-radio](case29-lora-radio/input.txt) | 7 | 5 | 9 | 0 | 9 |

## 测试时怎么走

1. **阶段一**：打开需求分析工作台 <http://localhost:8878/>，新建需求，把用例里的 `input.txt` 原文粘进对话框；按「阶段一预期输出」的检查点 A~D 核对。
2. **阶段二**：交接完成后，用 `tools/bridge_studio.py run --project <项目编号>` 把我的阶段接上去；或直接把用例里的 `requirement_spec.json` 跑一遍：

   ```bash
   python3 cli.py run --spec testcases/<case>/requirement_spec.json --out /tmp/r.json
   ```

3. 页面上展开每一行的「证据」，逐条对照本文档的预期。

---

## 测试用例：case31-key-sound-toy

### 阶段一输入（粘进需求分析工作台）

```text
设计一款按键发声玩具工程，采用CX588 OTP语音播放芯片（内置8位MCU、音频DAC和1.2W功放，支持PWM/DAC直接驱动扬声器，可存储多段语音）作为核心，搭配C10S01X单通道电容式触摸感应芯片实现触摸按键检测（灵敏度可调），由PW5100同步升压芯片（0.9V极低启动电压，3.3V固定输出，效率>94%）将单节AA/AAA干电池升压至3.3V供电管理，板载约22个元器件，集成按键矩阵输入接口和扬声器接口（支持8Ω/2W喇叭），支持6键独立触发多段语音/音效播放、单节1.5V干电池供电（0.9V~1.5V输入范围）、触摸按键唤醒触发和LED工作状态指示功能。

开源项目：https://oshwhub.com/ketos/key-sound-toy
```

### 阶段一预期输出

工作台会把它整理成需求清单与选型项。可核对的检查点：

- **明确提到的器件 2 个**，其中能在器件目录里确认身份的 0 个：


  以下出现在原文里但目录里没查到，应标成「待核验」而不是编造：

  - `CX588`
  - `PW5100`

- **检查点 A**：原文点名的器件必须出现在选型项的「指定/优先/参考」里，且意图不能升级成「必须」；
- **检查点 B**：原文里的功能点数（「支持」后面列的那串）应当逐条落到需求条目上，不能丢；
- **检查点 C**：查不到的器件保留为待核验，不得替换成相近型号；
- **检查点 D**：交接状态应是 `ready` 或 `ready_with_open_items`，不应是 `blocked`。

### 阶段二输入

用工作台真实交接件（`SPEC-029ca4360143491f9620b8361143f078-v16-d1`）转成的输入，已存为 `case31-key-sound-toy/requirement_spec.real.json`。

**A. 真实交接件跑出来的结果（当前实现的实际表现）**

- 选型项 **5**；给出推荐 **2**；未核对 **3**；阻塞 **3**。

| 选型项 | 指定器件 | 召回方式 | 候选数 | 预期结论 | 预期首选 |
| --- | --- | --- | --- | --- | --- |
| SEL-001 | CX588 | exact_model_miss:CX588 → leaf_code:l_cryptographic_coprocessor+6 | 12 | 未核对 | TMS320F2812PGFA、DSPIC33EP256MC204-I/PT |
| SEL-002 | PW5100 | exact_model_miss:PW5100 → leaf_code:l_ac_dc_controller_and_voltage_regulator+8 | 12 | 有推荐 | UC3843BD1013TR、NCP1252ADR2G |
| SEL-003 | C10S01X | exact_model_miss:C10S01X → leaf_code:l_capacitive_sensor_ic+2 | 11 | 未核对 | MC1081L、MC11S |
| SEL-004 | — | leaf_code:l_button_switch+16 | 12 | 未核对 | XKB8080-Z、XKB7070-Z |
| SEL-005 | CX588 | exact_model_miss:CX588 → leaf_code:l_audio_codec+3 | 12 | 有推荐 | ES8388、ES8316 |

**逐条判定（页面上展开「证据」逐条核对）：**

- SEL-002（单节电池升压供电） 首选 UC3843BD1013TR，结论 conditional：
  - 未知 `input_voltage`：器件记录中没有该字段
  - 未知 `output_voltage`：器件记录中没有该字段
  - 未知 `feature`：器件记录中没有该字段
- SEL-005（扬声器音频输出） 首选 ES8388，结论 conditional：
  - 未知 `feature`：器件记录中没有该字段

**应显示为非绿色的项：**

- SEL-001：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-003：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-004：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对

### 怎么判定通过

1. 阶段一检查点 A~D 全部成立（重点看有没有把「参考」升级成「必须」、有没有编造型号）；
2. 阶段二给出的推荐项，逐条展开证据应能指回器件目录记录或数据手册片段；
3. 标成「未核对」或「阻塞」的项，要能说清是缺条件、缺候选还是证据不足；
4. 不得出现「没有证据却显示为推荐」的绿色项。

（本用例的阶段二结果是实测导出，见 `case31-key-sound-toy/result.json`）
## 测试用例：case30-pn532-nfc

### 阶段一输入（粘进需求分析工作台）

```text
设计一款PN532 RFID/NFC调试器工程，采用PN5321A3HN（NXP 13.56MHz NFC读写芯片，集成80C51内核，支持ISO14443A/B、ISO18092、Mifare Classic/Ultralight、FeliCa和NTAG全系列协议）作为核心，搭配CH340N USB转UART串口芯片实现即插即用免驱通信，由RT9013双路LDO（1.2V内核+3.3V IO）提供低噪声稳压电源管理，板载约64个元器件，集成USB-C接口（供电与通信二合一）和4Pin调试排针扩展，支持13.56MHz高频RFID/NFC卡片读写操作、Mifare S50/S70/Ultralight/Desfire卡协议解析、NTAG21x系列标签读写、ISO14443A/B双协议自动识别、板载PCB天线（ANT）近距离感应和红蓝双色LED工作状态指示功能。

开源项目：https://oshwhub.com/yizhidianzi/pn532-rfid-deugger
```

### 阶段一预期输出

工作台会把它整理成需求清单与选型项。可核对的检查点：

- **明确提到的器件 7 个**，其中能在器件目录里确认身份的 4 个：

  - `PN532` → 目录型号 **PN5321A3HN/C106,55**（射频频率卡芯片，device_id=3230）
  - `PN5321A3HN` → 目录型号 **PN5321A3HN/C106,55**（射频频率卡芯片，device_id=3230）
  - `CH340N` → 目录型号 **CH340N**（USB转换芯片，device_id=7896）
  - `RT9013` → 目录型号 **RT9013-33GB**（线性稳压器LDO，device_id=8920）

  以下出现在原文里但目录里没查到，应标成「待核验」而不是编造：

  - `ISO14443A/B`
  - `ISO18092`
  - `NTAG21x`

- **检查点 A**：原文点名的器件必须出现在选型项的「指定/优先/参考」里，且意图不能升级成「必须」；
- **检查点 B**：原文里的功能点数（「支持」后面列的那串）应当逐条落到需求条目上，不能丢；
- **检查点 C**：查不到的器件保留为待核验，不得替换成相近型号；
- **检查点 D**：交接状态应是 `ready` 或 `ready_with_open_items`，不应是 `blocked`。

### 阶段二输入

用工作台真实交接件（`SPEC-95f3fa586ac84e71a763987388feae04-v12-d9`）转成的输入，已存为 `case30-pn532-nfc/requirement_spec.real.json`。

**A. 真实交接件跑出来的结果（当前实现的实际表现）**

- 选型项 **7**；给出推荐 **4**；未核对 **3**；阻塞 **3**。

| 选型项 | 指定器件 | 召回方式 | 候选数 | 预期结论 | 预期首选 |
| --- | --- | --- | --- | --- | --- |
| SEL-001 | PN5321A3HN | exact_model:PN5321A3HN | 2 | 有推荐 | PN5321A3HN/C106,55、PN5321A3HN/C106,51 |
| SEL-002 | CH340N | exact_model:CH340N | 1 | 有推荐 | CH340N |
| SEL-003 | — | leaf_code:l_ac_power_connector+48 | 12 | 有推荐 | RT-C14-C0B-AA、DB-8-6BS9 |
| SEL-004 | RT9013 | exact_model:RT9013 | 3 | 有推荐 | RT9013-33GB、RT9013-12GB |
| SEL-005 | — | leaf_code:l_cryptographic_coprocessor+6 | 12 | 未核对 | TMS320F2812PGFA、DSPIC33EP256MC204-I/PT |
| SEL-006 | — | leaf_code:l_antenna+4 | 12 | 未核对 | KH-RFECA3216060A1T_V09、ANT3216LL00R2400A |
| SEL-007 | — | leaf_code:l_digital_tube_driver+3 | 12 | 未核对 | TM1638、TM1640(TA2103) |

**逐条判定（页面上展开「证据」逐条核对）：**

- SEL-001（NFC/RFID读写与协议处理） 首选 PN5321A3HN/C106,55，结论 conditional：
  - 未知 `feature`：器件记录中没有该字段
  - 未知 `feature`：器件记录中没有该字段
- SEL-002（USB转UART通信模块） 首选 CH340N，结论 conditional：
  - 未知 `feature`：器件记录中没有该字段
- SEL-003（USB-C外部接口） 首选 RT-C14-C0B-AA，结论 conditional：
  - 未知 `feature`：器件记录中没有该字段
- SEL-004（板内稳压供电模块） 首选 RT9013-33GB，结论 conditional：
  - 未知 `feature`：器件记录中没有该字段
  - 未知 `feature`：器件记录中没有该字段

**应显示为非绿色的项：**

- SEL-005：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-006：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-007：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对

**B. 原文写明数字的加强版（用来测判定逻辑）**

在真实交接件上追加了这些条件（每条都标了原文出处）：

- SEL-004 `output_voltage == 3.3V` ← 由RT9013双路LDO（1.2V内核+3.3V IO）提供低噪声稳压电源管理

输入见 `case30-pn532-nfc/requirement_spec.manual.json`，结果：

- 选型项 **7**；给出推荐 **4**；未核对 **3**；阻塞 **3**。

| 选型项 | 指定器件 | 召回方式 | 候选数 | 预期结论 | 预期首选 |
| --- | --- | --- | --- | --- | --- |
| SEL-001 | PN5321A3HN | exact_model:PN5321A3HN | 2 | 有推荐 | PN5321A3HN/C106,55、PN5321A3HN/C106,51 |
| SEL-002 | CH340N | exact_model:CH340N | 1 | 有推荐 | CH340N |
| SEL-003 | — | leaf_code:l_ac_power_connector+48 | 12 | 有推荐 | RT-C14-C0B-AA、DB-8-6BS9 |
| SEL-004 | RT9013 | exact_model:RT9013 | 3 | 有推荐 | RT9013-33GB、RT9013-3.3V |
| SEL-005 | — | leaf_code:l_cryptographic_coprocessor+6 | 12 | 未核对 | TMS320F2812PGFA、DSPIC33EP256MC204-I/PT |
| SEL-006 | — | leaf_code:l_antenna+4 | 12 | 未核对 | KH-RFECA3216060A1T_V09、ANT3216LL00R2400A |
| SEL-007 | — | leaf_code:l_digital_tube_driver+3 | 12 | 未核对 | TM1638、TM1640(TA2103) |

**逐条判定（页面上展开「证据」逐条核对）：**

- SEL-001（NFC/RFID读写与协议处理） 首选 PN5321A3HN/C106,55，结论 conditional：
  - 未知 `feature`：器件记录中没有该字段
  - 未知 `feature`：器件记录中没有该字段
- SEL-002（USB转UART通信模块） 首选 CH340N，结论 conditional：
  - 未知 `feature`：器件记录中没有该字段
- SEL-003（USB-C外部接口） 首选 RT-C14-C0B-AA，结论 conditional：
  - 未知 `feature`：器件记录中没有该字段
- SEL-004（板内稳压供电模块） 首选 RT9013-33GB，结论 conditional：
  - 满足 `output_voltage`：器件 3.3V
  - 未知 `feature`：器件记录中没有该字段
  - 未知 `feature`：器件记录中没有该字段

**应显示为非绿色的项：**

- SEL-005：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-006：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-007：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对

### 怎么判定通过

1. 阶段一检查点 A~D 全部成立（重点看有没有把「参考」升级成「必须」、有没有编造型号）；
2. 阶段二给出的推荐项，逐条展开证据应能指回器件目录记录或数据手册片段；
3. 标成「未核对」或「阻塞」的项，要能说清是缺条件、缺候选还是证据不足；
4. 不得出现「没有证据却显示为推荐」的绿色项。

（本用例的阶段二结果是实测导出，见 `case30-pn532-nfc/result.json`）
## 测试用例：case25-stm32g4-foc

### 阶段一输入（粘进需求分析工作台）

```text
设计一款基于STM32G4的FOC电机驱动学习板工程，采用STM32G431RBT6（Cortex-M4，170MHz，集成硬件DSP指令和单精度FPU浮点单元、高分辨率PWM定时器、12位5MSPS高速ADC和内置比较器，专为数字电源和电机控制优化）作为主控，搭配NCE7560K大功率N-MOSFET（75V/60A，极低Rds(on) 6.5mΩ）构建三相全桥逆变驱动电路，配备FMMT593高压PNP晶体管辅助驱动、W25Q16JVUXIQ 2MB SPI Flash存储配置参数，通过RS624XQ四通道轨到轨运放实现三相低边电流采样，板载约334个元器件，集成6个外设接口，支持三相BLDC无刷直流电机和PMSM永磁同步电机无感FOC矢量控制（含滑模观测器）、CAN总线通信（TJA1051TK/3高速CAN收发器）、GS4157B模拟开关通道自动切换、LM321运放单相电流采样、TS-KG02S继电器输出控制、XT30PW和MR30PW大电流电源接口（兼容3S~6S锂电池）以及编码器/霍尔传感器接口。

开源项目：https://oshwhub.com/lylssy/foc_driver
```

### 阶段一预期输出

工作台会把它整理成需求清单与选型项。可核对的检查点：

- **明确提到的器件 12 个**，其中能在器件目录里确认身份的 6 个：

  - `STM32G4` → 目录型号 **STM32G431RBT6**（微控制器、微处理器与SoC，device_id=9000）
  - `STM32G431RBT6` → 目录型号 **STM32G431RBT6**（微控制器、微处理器与SoC，device_id=9000）
  - `RS624XQ` → 目录型号 **RS624XQ**（运算放大器，device_id=8857）
  - `TJA1051TK/3` → 目录型号 **TJA1051TK/3/1J**（CAN收发器，device_id=7691）
  - `GS4157B` → 目录型号 **GS4157B-CR**（模拟开关多路复用器，device_id=8941）
  - `LM321` → 目录型号 **LM321**（运算放大器，device_id=6673）

  以下出现在原文里但目录里没查到，应标成「待核验」而不是编造：

  - `Cortex-M4`
  - `NCE7560K`
  - `FMMT593`
  - `TS-KG02S`
  - `XT30PW`
  - `MR30PW`

- **检查点 A**：原文点名的器件必须出现在选型项的「指定/优先/参考」里，且意图不能升级成「必须」；
- **检查点 B**：原文里的功能点数（「支持」后面列的那串）应当逐条落到需求条目上，不能丢；
- **检查点 C**：查不到的器件保留为待核验，不得替换成相近型号；
- **检查点 D**：交接状态应是 `ready` 或 `ready_with_open_items`，不应是 `blocked`。

### 阶段二输入

用工作台真实交接件（`SPEC-d942b4d6c43448bf9a38c4155106ed62-v12-d12`）转成的输入，已存为 `case25-stm32g4-foc/requirement_spec.real.json`。

**A. 真实交接件跑出来的结果（当前实现的实际表现）**

- 选型项 **13**；给出推荐 **0**；未核对 **13**；阻塞 **13**。

| 选型项 | 指定器件 | 召回方式 | 候选数 | 预期结论 | 预期首选 |
| --- | --- | --- | --- | --- | --- |
| SEL-001 | STM32G431RBT6 | exact_model:STM32G431RBT6 | 1 | 未核对 | STM32G431RBT6 |
| SEL-002 | NCE7560K | exact_model_miss:NCE7560K → leaf_code:l_brush_dc_motor_drive_chip+8 | 12 | 未核对 | TC118S、DRV8837DSGR |
| SEL-003 | FMMT593 | exact_model_miss:FMMT593 → leaf_code:l_brush_dc_motor_drive_chip+8 | 12 | 未核对 | TC118S、DRV8837DSGR |
| SEL-004 | RS624XQ | exact_model:RS624XQ | 1 | 未核对 | RS624XQ |
| SEL-005 | GS4157B | exact_model:GS4157B | 1 | 未核对 | GS4157B-CR |
| SEL-006 | LM321 | exact_model:LM321 | 1 | 未核对 | LM321 |
| SEL-007 | TJA1051TK/3 | exact_model:TJA1051TK/3 | 1 | 未核对 | TJA1051TK/3/1J |
| SEL-008 | TS-KG02S | exact_model_miss:TS-KG02S → leaf_code:l_brush_dc_motor_drive_chip+8 | 12 | 未核对 | TC118S、DRV8837DSGR |
| SEL-009 | W25Q16JVUXIQ | exact_model:W25Q16JVUXIQ | 1 | 未核对 | W25Q16JVUXIQ |
| SEL-010 | XT30PW | exact_model_miss:XT30PW → leaf_code:l_ac_power_connector+48 | 12 | 未核对 | RT-C14-C0B-AA、DB-8-6BS9 |
| SEL-011 | — | leaf_code:l_magnetic_angle_sensor+3 | 12 | 未核对 | TLE5012BE1000、MT6825GT-STD |
| SEL-012 | — | leaf_code:l_ac_power_connector+48 | 12 | 未核对 | RT-C14-C0B-AA、DB-8-6BS9 |
| SEL-013 | — | leaf_code:l_ac_dc_controller_and_voltage_regulator+8 | 12 | 未核对 | SG3525AP013TR、UC3843BD1013TR |

**应显示为非绿色的项：**

- SEL-001：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-002：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-003：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-004：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-005：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-006：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-007：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-008：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-009：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-010：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-011：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-012：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-013：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对

**B. 原文写明数字的加强版（用来测判定逻辑）**

在真实交接件上追加了这些条件（每条都标了原文出处）：

- SEL-004 `channel_count >= 4` ← 通过RS624XQ四通道轨到轨运放实现三相低边电流采样

输入见 `case25-stm32g4-foc/requirement_spec.manual.json`，结果：

- 选型项 **13**；给出推荐 **1**；未核对 **12**；阻塞 **12**。

| 选型项 | 指定器件 | 召回方式 | 候选数 | 预期结论 | 预期首选 |
| --- | --- | --- | --- | --- | --- |
| SEL-001 | STM32G431RBT6 | exact_model:STM32G431RBT6 | 1 | 未核对 | STM32G431RBT6 |
| SEL-002 | NCE7560K | exact_model_miss:NCE7560K → leaf_code:l_brush_dc_motor_drive_chip+8 | 12 | 未核对 | TC118S、DRV8837DSGR |
| SEL-003 | FMMT593 | exact_model_miss:FMMT593 → leaf_code:l_brush_dc_motor_drive_chip+8 | 12 | 未核对 | TC118S、DRV8837DSGR |
| SEL-004 | RS624XQ | exact_model:RS624XQ | 1 | 有推荐 | RS624XQ |
| SEL-005 | GS4157B | exact_model:GS4157B | 1 | 未核对 | GS4157B-CR |
| SEL-006 | LM321 | exact_model:LM321 | 1 | 未核对 | LM321 |
| SEL-007 | TJA1051TK/3 | exact_model:TJA1051TK/3 | 1 | 未核对 | TJA1051TK/3/1J |
| SEL-008 | TS-KG02S | exact_model_miss:TS-KG02S → leaf_code:l_brush_dc_motor_drive_chip+8 | 12 | 未核对 | TC118S、DRV8837DSGR |
| SEL-009 | W25Q16JVUXIQ | exact_model:W25Q16JVUXIQ | 1 | 未核对 | W25Q16JVUXIQ |
| SEL-010 | XT30PW | exact_model_miss:XT30PW → leaf_code:l_ac_power_connector+48 | 12 | 未核对 | RT-C14-C0B-AA、DB-8-6BS9 |
| SEL-011 | — | leaf_code:l_magnetic_angle_sensor+3 | 12 | 未核对 | TLE5012BE1000、MT6825GT-STD |
| SEL-012 | — | leaf_code:l_ac_power_connector+48 | 12 | 未核对 | RT-C14-C0B-AA、DB-8-6BS9 |
| SEL-013 | — | leaf_code:l_ac_dc_controller_and_voltage_regulator+8 | 12 | 未核对 | SG3525AP013TR、UC3843BD1013TR |

**逐条判定（页面上展开「证据」逐条核对）：**

- SEL-004（三相低边电流采样模块） 首选 RS624XQ，结论 conditional：
  - 未知 `channel_count`：该字段标记为缺失

**应显示为非绿色的项：**

- SEL-001：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-002：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-003：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-005：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-006：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-007：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-008：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-009：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-010：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-011：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-012：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-013：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对

### 怎么判定通过

1. 阶段一检查点 A~D 全部成立（重点看有没有把「参考」升级成「必须」、有没有编造型号）；
2. 阶段二给出的推荐项，逐条展开证据应能指回器件目录记录或数据手册片段；
3. 标成「未核对」或「阻塞」的项，要能说清是缺条件、缺候选还是证据不足；
4. 不得出现「没有证据却显示为推荐」的绿色项。

（本用例的阶段二结果是实测导出，见 `case25-stm32g4-foc/result.json`）
## 测试用例：case29-lora-radio

### 阶段一输入（粘进需求分析工作台）

```text
设计一款基于LoRa的无线通信装置工程，采用ESP32-WROOM-32E（Xtensa LX6双核处理器，240MHz，8MB SPI Flash，支持Wi-Fi 4和蓝牙4.2+BLE）作为主控，搭配E32-433T30D LoRa扩频无线串口模块（基于SX1278芯片，433MHz频段，最大30dBm发射功率，空中速率0.3~19.2kbps，理论通信距离8km）实现远距离数据传输，由TP4056X线性锂电池充电管理芯片（最大1A充电电流）和SX1308 boost升压芯片（2A开关电流，最高28V输出）组成电源系统，板载约82个元器件，集成3个外设接口，支持433MHz LoRa远距离扩频透明传输、WiFi+蓝牙双模本地无线网关桥接、IP3005A集成电量计（4段LED电量指示）、SPX3819超低噪声LDO稳压（3.3V输出）、S8050和AO3401A MOSFET开关控制、IPEX天线接口、USB-C充电/通信二合一接口和18650锂电池座供电功能。

开源项目：https://oshwhub.com/robba/wireless-communication-device-based-on-lora
```

### 阶段一预期输出

工作台会把它整理成需求清单与选型项。可核对的检查点：

- **明确提到的器件 7 个**，其中能在器件目录里确认身份的 5 个：

  - `ESP32-WROOM-32E` → 目录型号 **ESP32-WROOM-32E-N4**（Wi-Fi模块，device_id=4654）
  - `TP4056X` → 目录型号 **TP4056X-42-ESOP8**（电池管理，device_id=8962）
  - `SX1308` → 目录型号 **SX1308**（升压与升降压DC-DC转换器，device_id=8939）
  - `IP3005A` → 目录型号 **IP3005A**（电池管理，device_id=8852）
  - `SPX3819` → 目录型号 **SPX3819M5-L-3-3/TR**（线性稳压器LDO，device_id=5305）

  以下出现在原文里但目录里没查到，应标成「待核验」而不是编造：

  - `SX1278`
  - `AO3401A`

- **检查点 A**：原文点名的器件必须出现在选型项的「指定/优先/参考」里，且意图不能升级成「必须」；
- **检查点 B**：原文里的功能点数（「支持」后面列的那串）应当逐条落到需求条目上，不能丢；
- **检查点 C**：查不到的器件保留为待核验，不得替换成相近型号；
- **检查点 D**：交接状态应是 `ready` 或 `ready_with_open_items`，不应是 `blocked`。

### 阶段二输入

用工作台真实交接件（`SPEC-5f471c5ea0df471484e1c0efecd4a381-v8-d21`）转成的输入，已存为 `case29-lora-radio/requirement_spec.real.json`。

**A. 真实交接件跑出来的结果（当前实现的实际表现）**

- 选型项 **9**；给出推荐 **0**；未核对 **9**；阻塞 **9**。

| 选型项 | 指定器件 | 召回方式 | 候选数 | 预期结论 | 预期首选 |
| --- | --- | --- | --- | --- | --- |
| SEL-001 | ESP32-WROOM-32E | exact_model:ESP32-WROOM-32E | 5 | 未核对 | ESP32-WROOM-32E-N4、ESP32-WROOM-32E-N8 |
| SEL-002 | E32-433T30D | exact_model:E32-433T30D | 1 | 未核对 | E32-433T30D |
| SEL-003 | TP4056X | exact_model:TP4056X | 1 | 未核对 | TP4056X-42-ESOP8 |
| SEL-004 | SX1308 | exact_model:SX1308 | 1 | 未核对 | SX1308 |
| SEL-005 | SPX3819 | exact_model:SPX3819 | 2 | 未核对 | SPX3819M5-L-3-3/TR、SPX3819M5-L-1-5/TR |
| SEL-006 | IP3005A | exact_model:IP3005A | 1 | 未核对 | IP3005A |
| SEL-007 | S8050 | exact_model:S8050 | 1 | 未核对 | S8050 |
| SEL-008 | — | leaf_code:l_ac_power_connector+48 | 12 | 未核对 | RT-C14-C0B-AA、DB-8-6BS9 |
| SEL-009 | — | leaf_code:l_antenna+4 | 12 | 未核对 | KH-RFECA3216060A1T_V09、ANT3216LL00R2400A |

**应显示为非绿色的项：**

- SEL-001：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-002：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-003：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-004：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-005：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-006：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-007：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-008：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-009：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对

**B. 原文写明数字的加强版（用来测判定逻辑）**

在真实交接件上追加了这些条件（每条都标了原文出处）：

- SEL-005 `output_voltage == 3.3V` ← SPX3819超低噪声LDO稳压（3.3V输出）
- SEL-003 `maximum_charging_current >= 1A` ← 由TP4056X线性锂电池充电管理芯片（最大1A充电电流）
- SEL-004 `output_current >= 2A` ← SX1308 boost升压芯片（2A开关电流，最高28V输出）

输入见 `case29-lora-radio/requirement_spec.manual.json`，结果：

- 选型项 **9**；给出推荐 **3**；未核对 **6**；阻塞 **6**。

| 选型项 | 指定器件 | 召回方式 | 候选数 | 预期结论 | 预期首选 |
| --- | --- | --- | --- | --- | --- |
| SEL-001 | ESP32-WROOM-32E | exact_model:ESP32-WROOM-32E | 5 | 未核对 | ESP32-WROOM-32E-N4、ESP32-WROOM-32E-N8 |
| SEL-002 | E32-433T30D | exact_model:E32-433T30D | 1 | 未核对 | E32-433T30D |
| SEL-003 | TP4056X | exact_model:TP4056X | 1 | 有推荐 | TP4056X-42-ESOP8 |
| SEL-004 | SX1308 | exact_model:SX1308 | 1 | 有推荐 | SX1308 |
| SEL-005 | SPX3819 | exact_model:SPX3819 | 2 | 有推荐 | SPX3819M5-L-3-3/TR |
| SEL-006 | IP3005A | exact_model:IP3005A | 1 | 未核对 | IP3005A |
| SEL-007 | S8050 | exact_model:S8050 | 1 | 未核对 | S8050 |
| SEL-008 | — | leaf_code:l_ac_power_connector+48 | 12 | 未核对 | RT-C14-C0B-AA、DB-8-6BS9 |
| SEL-009 | — | leaf_code:l_antenna+4 | 12 | 未核对 | KH-RFECA3216060A1T_V09、ANT3216LL00R2400A |

**逐条判定（页面上展开「证据」逐条核对）：**

- SEL-003（锂电池线性充电模块） 首选 TP4056X-42-ESOP8，结论 recommended：
  - 满足 `maximum_charging_current`：器件 1A
- SEL-004（升压电源模块） 首选 SX1308，结论 recommended：
  - 满足 `output_current`：器件 2A
- SEL-005（3.3V LDO稳压模块） 首选 SPX3819M5-L-3-3/TR，结论 recommended：
  - 满足 `output_voltage`：器件 3.3V

**应显示为非绿色的项：**

- SEL-001：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-002：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-006：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-007：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-008：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对
- SEL-009：该选型项没有可判定的条件，候选只来自类别召回，未做参数核对

### 怎么判定通过

1. 阶段一检查点 A~D 全部成立（重点看有没有把「参考」升级成「必须」、有没有编造型号）；
2. 阶段二给出的推荐项，逐条展开证据应能指回器件目录记录或数据手册片段；
3. 标成「未核对」或「阻塞」的项，要能说清是缺条件、缺候选还是证据不足；
4. 不得出现「没有证据却显示为推荐」的绿色项。

（本用例的阶段二结果是实测导出，见 `case29-lora-radio/result.json`）
