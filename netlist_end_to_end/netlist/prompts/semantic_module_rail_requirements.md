以下是已冻结rail端口必须满足的同网义务，不是额外器件或新设计选择。voltage_domain与同名module_port是两个不同端点，声明名字相同不会连接。你必须在SCL明确连接这些端点，或通过已声明导线使其同网；允许connect(module_port(...),已声明的电压域句柄)。logic端口不属于这些义务，不能短接电源。
