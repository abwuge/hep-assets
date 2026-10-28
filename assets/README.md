# 共享资产

| 目录 | 内容 |
| --- | --- |
| `data/demo_flux/` | AMS 通量分析的六个 ROOT 输入文件 |
| `references/demo_flux/` | 数据说明、方法规范及参考源码 |
| `tools/demo_flux/` | flux 所需科学软件的安装材料与用法 |

数据定义与单位见 [Data_README.md](references/demo_flux/Data_README.md)，分析方法见[模板拟合说明](references/demo_flux/Template_Fitting_Method.md)和[效率修正说明](references/demo_flux/Efficiency_Correction_Method.md)。软件安装见[使用说明](tools/demo_flux/README.md)。

智能体通过分析服务读取这些只读资产。新增数据放入 `data/`，参考资料放入 `references/`，工具放入 `tools/`；本目录中的内容均向分析任务开放。
