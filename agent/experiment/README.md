# ISO 15118-4 TTCN-3 代码生成实验 Pipeline

本 Pipeline 用于评估 Code Agent 生成的 TTCN-3 代码与 ISO 15118-4 标准代码的差异，通过量化评分和详细报告帮助你了解代码生成的质量。

## 目录结构

```
experiment/
├── test_cases_config.json          # 测试用例配置文件
├── run_experiment.py               # Pipeline 主入口
├── code_generator.py               # 代码生成模块
├── standard_extractor.py           # 标准代码提取模块（整合版）
├── code_comparator.py              # 代码对比分析模块
├── report_generator.py             # 报告生成模块
├── evaluation_pipeline.py           # 评估 Pipeline
├── results/                        # 实验结果目录
│   ├── TC_SECC_CMN_VTB_SupportedAppProtocol_001/
│   │   ├── generated.ttcn         # Agent 生成的代码
│   │   ├── standard.ttcn          # 标准参考代码
│   │   ├── comparison_report.txt  # 对比分析报告
│   │   ├── summary.json           # JSON 摘要
│   │   └── experiment_result.json # 实验结果元数据
│   └── ...
└── README.md                       # 本文件
```

## 快速开始

### 1. 单个测试用例实验

```bash
cd /data/cdy/code/v2g2_082/iso15118/agent/experiment

# 运行完整流程（代码生成 + 标准提取 + 对比分析 + 报告生成）
python run_experiment.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001

# 跳过代码生成（使用已有代码）
python run_experiment.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001 --skip-generation

# 覆盖已有结果
python run_experiment.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001 --overwrite
```

### 2. 批量测试

```bash
# 批量运行（从配置文件读取测试用例列表）
python run_experiment.py --batch

# 指定配置文件
python run_experiment.py --batch --config ./test_cases_config.json
```

### 3. 仅运行对比

如果已经有生成代码和标准代码文件，可以直接运行对比：

```bash
python run_experiment.py --compare-only \
    --generated /path/to/generated.ttcn \
    --standard /path/to/standard.ttcn \
    --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001
```

## 配置说明

### test_cases_config.json

```json
{
  "single_test": {
    "enabled": true,
    "test_case": {
      "tc_id": "TC_SECC_CMN_VTB_SupportedAppProtocol_001",
      "description": "SECC SupportedAppProtocol GoodCase"
    }
  },
  "batch_test": {
    "enabled": true,
    "test_cases": [
      {"tc_id": "TC_SECC_CMN_VTB_SessionSetup_001"},
      {"tc_id": "TC_SECC_CMN_VTB_V2GTPHeader_001"}
    ]
  },
  "pipeline_config": {
    "steps": {
      "code_generation": {"enabled": true},
      "standard_extraction": {"enabled": true},
      "code_comparison": {"enabled": true},
      "report_generation": {"enabled": true}
    },
    "results_dir": "./results"
  }
}
```

## 评分体系

Pipeline 使用多维度评分体系评估代码质量：

| 维度 | 权重 | 说明 |
|-----|------|------|
| 结构完整性 | 30% | module、import、testcase 等元素的存在性 |
| 命名规范性 | 20% | 元素命名是否符合 ISO 15118-4 规范 |
| 语法正确性 | 30% | alt 块、定时器、verdict 等关键语法 |
| 元素完整性 | 20% | 缺失/多余的函数、模板等 |

评分结果分为以下等级：

- **90-100**: 优秀 - 代码质量与标准高度一致
- **70-89**: 良好 - 存在少量差异，可小幅优化
- **50-69**: 一般 - 需要关注问题并进行修复
- **0-49**: 较差 - 建议重新生成

## 输出文件说明

### 1. comparison_report.txt

文本格式的对比报告，包含：
- 综合相似度得分
- 各维度指标得分（带可视化进度条）
- 代码统计信息（行数、组件数等）
- 问题列表

### 2. summary.json

JSON 格式的摘要，便于程序化处理，包含：
- 各维度得分
- 差异列表
- 缺失/多余元素

### 3. generated.ttcn

Code Agent 生成的 TTCN-3 代码副本

### 4. standard.ttcn

从 ISO 15118-4 知识库提取的标准参考代码

## 添加新测试用例

### 方法 1: 修改配置文件

编辑 `test_cases_config.json`，在 `batch_test.test_cases` 中添加新的测试用例：

```json
{
  "batch_test": {
    "test_cases": [
      {
        "tc_id": "TC_SECC_CMN_VTB_YourNewTest_001",
        "description": "描述你的测试用例"
      }
    ]
  }
}
```

### 方法 2: 命令行直接指定

```bash
python run_experiment.py --tc-id TC_SECC_CMN_VTB_YourNewTest_001
```

## 扩展 Pipeline

### 添加新的对比维度

在 `code_comparator.py` 中，可以添加新的对比维度：

```python
class CodeComparator:
    def __init__(self, ...):
        self.comparison_levels = ["structure", "naming", "syntax", "completeness", "new_dimension"]

    def _compare_new_dimension(self):
        # 实现新的对比逻辑
        ...
```

### 自定义报告格式

在 `report_generator.py` 中，可以添加新的报告格式：

```python
class CustomReportGenerator(ReportGenerator):
    def generate(self, data, output_path):
        # 实现自定义报告生成逻辑
        ...
```

## 常见问题

### Q: 标准代码提取失败怎么办？

A: 检查知识库文件是否存在：
```bash
ls -la ../document/iso15118_4_ttcn_knowledge_base.json
```

### Q: 代码生成超时怎么办？

A: 默认超时为 300 秒，可通过修改 `code_generator.py` 中的 `timeout` 参数调整。

### Q: 如何查看详细日志？

A: 设置环境变量或修改配置文件中的 `log_level` 为 `DEBUG`。

## 维护说明

- Pipeline 版本：1.0.0
- 创建日期：2026-04-10
- 依赖：
  - Python 3.8+
  - 标准库：json, re, pathlib, dataclasses

## 联系方式

如有问题或建议，请联系 Code Agent Team。
