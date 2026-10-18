只输出 SCL 源码，不输出 JSON、Markdown 围栏、RESULT 标签或解释。
器件别名由 context.modules[当前模块].devices 预绑定；绝不创建器件、修改取值或脚号。
唯一操作：
G = voltage_domain_ref("frozen_ground_id")
V = voltage_domain_ref("frozen_rail_id")
connect(pin(U1, "1"), V)
U1.connect_device_pin("2", R1, "1")
connect(pin(R1, "2"), G)
connect(pin(U1, "3"), module_port("module_id", "port_id", bit=0))
connect(module_port("module_id", "power_in", bit=0), V)
上述型号、脚号、数值只是语法示例，必须使用本任务冻结 context 的实际数据。
最后一行仅适用于冻结端口明确domain_binding=rail且domain对应V的情况；同名端口和电压域不会自动连接，domain_binding=logic的信号绝不能因此接到V。
已有冻结电压域优先用 voltage_domain_ref("精确域ID") 取得局部端点。它只接受一个非空字面量ID，不接受voltage_v/reference/profile_ref；不能定义或改变域，也不会自动生成连线。该ID必须在本次共同编译的voltage_domain声明中真实存在，否则拒绝导出；声明在前或在后均可。
旧voltage_domain("域ID",voltage_v=...,reference=...)声明语法仍有效，但只能重复plan.voltage_domains批准的完整定义；有profile_ref时必须原样传入且voltage_v=None，不能改成标称值。引用保留原voltage/reference/profile，不把未知变已知。
reference 只是电压参考元数据，不会生成地线。相同全局域 ID 才共享电源网络。
两个不同域不能直接短接，即使电压相同；电阻、磁珠等器件内部不会被当成导线。
可以用 P = pin(U1,"1") 或 P = module_port(...) 绑定端点句柄，不能重定义句柄。
局部只能访问本模块的器件和端口。voltage_domain_ref仅限局部；顶层只能 connect 两个不同模块的 module_port。
每次返回完整替换的本模块SCL，不是增量补丁。host独立提供冻结域声明；从本模块替换文本中省略旧的重复域定义不会删除这些冻结域。其他需要的端点绑定和显式连线仍须完整返回。
禁止 import、循环、条件、with、属性读写、函数定义、任意函数调用、列表与字典构造。
每个未被上下文明确允许悬空的脚必须连接；每个端口 bit 必须绑定本模块真实脚。
external=false 的端口还需要显式顶层跨模块连接。不能添加端口来掩盖漏接。
无连接需要输出时只写注释 # No connections required，不能输出空响应或 pass。生成前检查全部脚和端口覆盖。