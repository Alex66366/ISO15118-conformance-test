# 测试用例生成系统重构计划

## 目标
1. 改进预处理器 `iso15118_preprocessor_enhanced.py`，增加 figure/table 引用检索
2. 改进 `iso15118_2_hierarchical_tree.json` 结构，增加 figure/table 关联
3. 将测试用例生成器从 OpenAI API 调用改为 Claude Code Agent 模式
4. 增加多条需求生成一个测试用例的能力

## 修改文件

### 1. `document/iso15118_preprocessor_enhanced.py`
- 增加 figure/table 引用提取功能
- 为每个需求关联引用的 figure 和 table
- 增加 figure/table 索引构建（支持多页 figure）
- 输出 JSON 中增加 `figure_references` 和 `table_references` 字段

### 2. `test_case/test_case_generator.py` → 完全重写
- 移除 OpenAI API 依赖
- 改为 Claude Code Agent 模式（subprocess 调用 claude CLI）
- 增加单需求和多需求两种模式
- 增加 figure/table 自动检索并注入 prompt
- 参考 `test_code/ttcn3_generation_pipeline.py` 的 Agent 调用模式

### 3. 重新生成 `document/iso15118_2_hierarchical_tree.json`
- 运行改进后的预处理器生成新的 JSON
