import os
import json
from typing import Optional, Dict, Any
from django.conf import settings

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


class LLMClient:
    """LLM客户端，用于生成TTCN-3测试脚本"""

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model
        self.client = None
        if OpenAI and self.api_key:
            self.client = OpenAI(api_key=self.api_key)

    def is_available(self) -> bool:
        """检查LLM客户端是否可用"""
        return self.client is not None

    def generate_ttcn3_script(self, test_intent: str, test_type: str = "CMN") -> Dict[str, Any]:
        """
        生成TTCN-3测试脚本

        Args:
            test_intent: 自然语言测试意图描述
            test_type: 测试类型 (CMN/AC/DC)

        Returns:
            包含生成结果的字典
        """
        if not self.is_available():
            return {
                "status": "error",
                "message": "LLM client not initialized. Please set OPENAI_API_KEY environment variable.",
                "code": ""
            }

        prompt = self._build_prompt(test_intent, test_type)

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._get_system_prompt()},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2,
                max_tokens=4000
            )

            generated_code = response.choices[0].message.content

            return {
                "status": "success",
                "message": "TTCN-3 script generated successfully",
                "code": generated_code,
                "model": self.model
            }

        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to generate script: {str(e)}",
                "code": ""
            }

    def _get_system_prompt(self) -> str:
        """获取系统提示词"""
        return """你是一个专业的ISO 15118协议测试专家，擅长编写TTCN-3测试脚本。

你需要根据用户提供的自然语言测试意图，生成符合以下规范的TTCN-3测试代码：

1. 测试模块结构：
   - module名称使用有意义的命名
   - import必要的类型定义
   - type定义使用record/record-of/enumerated等

2. 测试用例结构：
   - testcase名称清晰表达测试目的
   - 包含适当的注释说明测试步骤
   - 使用合适的超时定时器
   - 正确处理V2GTP消息头

3. Template定义：
   - 清晰定义请求和响应模板
   - 包含必要的字段匹配规则

4. Altstep状态机：
   - 用于处理多个可选步骤
   - 包含超时和错误处理

5. ISO 15118协议特定：
   - 正确处理V2GTP协议版本
   - 支持SessionSetup, ServiceDiscovery, Authorization等消息
   - 考虑PICS/PIXIT配置

请生成完整、可编译的TTCN-3代码，包含所有必要的类型定义、模板和测试用例。"""

    def _build_prompt(self, test_intent: str, test_type: str) -> str:
        """构建用户提示词"""
        prompt_template = f"""请为以下测试场景生成TTCN-3测试脚本：

测试意图：{test_intent}
测试类型：{test_type}

要求：
1. 生成完整可用的TTCN-3代码
2. 包含类型定义、模板定义和测试用例
3. 正确处理V2GTP协议消息
4. 包含适当的日志输出和错误处理
5. 考虑与现有测试框架的兼容性

请生成代码："""

        return prompt_template


class LLMConfig:
    """LLM配置管理"""

    @staticmethod
    def get_default_config() -> Dict[str, Any]:
        """获取默认配置"""
        return {
            "model": os.getenv("LLM_MODEL", "gpt-4"),
            "temperature": 0.2,
            "max_tokens": 4000,
            "api_key": os.getenv("OPENAI_API_KEY", "")
        }

    @staticmethod
    def validate_config(config: Dict[str, Any]) -> tuple[bool, str]:
        """验证配置"""
        if not config.get("api_key") and not os.getenv("OPENAI_API_KEY"):
            return False, "API key is required"

        if config.get("model") not in ["gpt-4", "gpt-4-turbo", "gpt-3.5-turbo", "gpt-4o", "gpt-4o-mini"]:
            return False, "Unsupported model"

        return True, "OK"
