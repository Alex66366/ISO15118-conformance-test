#!/usr/bin/env python3
"""
ISO 15118-4 文档预处理器
从 ISO 15118-4 Markdown 文档中提取 TTCN-3 代码样例、测试用例描述和知识库

功能：
1. 提取 test case descriptions（测试用例描述表格）
2. 提取 TTCN-3 代码片段（testcase, function, template, altstep, type）
3. 构建 TTCN-3 知识库用于后续代码生成
4. 提取命名规范、端口架构、组件定义
"""

import re
import json
import os
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class TTCNCodeBlock:
    """TTCN-3 代码块"""
    code_type: str  # testcase, function, template, altstep, module, type
    name: str
    code: str
    source_section: str  # 来源章节 (e.g., "Annex C", "Annex E")
    related_message: str = ""  # 关联的消息类型 (e.g., "SessionSetup", "V2GTP")
    sut_type: str = ""  # SECC or EVCC
    category: str = ""  # CMN, AC, DC

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code_type": self.code_type,
            "name": self.name,
            "code": self.code,
            "source_section": self.source_section,
            "related_message": self.related_message,
            "sut_type": self.sut_type,
            "category": self.category,
        }


@dataclass
class TestCaseDescription:
    """测试用例描述"""
    tc_id: str
    test_objective: str
    document_reference: str
    referenced_requirements: List[str]
    config_id: str
    pics_selection: str
    pixit_selection: str
    precondition: str
    expected_behavior: str
    sut_type: str = ""  # SECC or EVCC
    message_type: str = ""  # SessionSetup, ServiceDiscovery, etc.
    test_type: str = ""  # GoodCase, BadCase

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tc_id": self.tc_id,
            "test_objective": self.test_objective,
            "document_reference": self.document_reference,
            "referenced_requirements": self.referenced_requirements,
            "config_id": self.config_id,
            "pics_selection": self.pics_selection,
            "pixit_selection": self.pixit_selection,
            "precondition": self.precondition,
            "expected_behavior": self.expected_behavior,
            "sut_type": self.sut_type,
            "message_type": self.message_type,
            "test_type": self.test_type,
        }


class ISO15118_4_Preprocessor:
    """ISO 15118-4 文档预处理器"""

    def __init__(self, doc_parts: List[str]):
        """
        Args:
            doc_parts: ISO 15118-4 Markdown 文件路径列表（3部分）
        """
        self.doc_parts = doc_parts
        self.full_content = ""
        self.test_case_descriptions: List[TestCaseDescription] = []
        self.ttcn_code_blocks: List[TTCNCodeBlock] = []

        # 命名规范知识库
        self.naming_conventions = {
            "testcase": "TC_[SECC|EVCC]_[CMN|AC|DC]_VTB_[MessageType]_NNN",
            "precondition_func": "f_[SECC|EVCC]_[CMN|AC|DC]_PR_[Name]_NNN",
            "test_behavior_func": "f_[SECC|EVCC]_[CMN|AC|DC]_TB_VTB_[MessageType]_NNN",
            "postcondition_func": "f_[SECC|EVCC]_[CMN|AC|DC]_PO_[Name]_NNN",
            "common_template": "md_CMN_[Name]_NNN",
            "secc_template": "mdw_SECC_[Name]_NNN",
            "evcc_template": "mdw_EVCC_[Name]_NNN",
            "altstep": "a_[SECC|EVCC]_[Name]",
            "timer_param": "par_V2G_[EVCC|SECC]_[Name]",
            "component_var": "vc_[Name]",
        }

        # 组件架构
        self.component_architecture = {
            "SECCTester": {
                "description": "Main Test Component for SECC tests",
                "ports": [
                    "pt_V2G_TCP_TLS_ALM_SECC_Port",
                    "pt_V2G_UDP_SDP_Port",
                    "pt_HAL_61851_Port",
                    "pt_HAL_61851_Internal_Port",
                ],
                "system": "SystemSECC",
            },
            "EVCCTester": {
                "description": "Main Test Component for EVCC tests",
                "ports": [
                    "pt_V2G_TCP_TLS_ALM_EVCC_Port",
                    "pt_V2G_UDP_SDP_Port",
                    "pt_HAL_61851_Port",
                    "pt_HAL_61851_Internal_Port",
                ],
                "system": "SystemEVCC",
            },
            "HAL_61851_Listener": {
                "description": "Parallel Test Component for IEC 61851 monitoring",
                "ports": ["pt_HAL_61851_Port"],
                "system": None,
            },
        }

        # 测试执行四阶段模型
        self.test_phases = {
            "phase_a_initialize": [
                "Create PTC components",
                "Map all MTC/PTC/system ports and communication bindings",
            ],
            "phase_b_precondition": [
                "Bring SUT and tester to a valid and known state",
            ],
            "phase_c_test_behavior": [
                "Initialize and start relevant timer(s)",
                "Execute test behavior (send stimulus to SUT)",
                "Listen to events and verify SUT response (stop timers)",
                "Assign test verdict",
            ],
            "phase_d_postcondition": [
                "Bring SUT and tester into final/initial state",
                "Shutdown PTC components",
            ],
        }

        # 标准 timer 配置
        self.timer_config = {
            "par_CMN_Transmission_Delay": 0.25,
            "par_CMN_HAL_Timeout": 2.0,
            "par_V2G_SECC_Sequence_Timeout": 60.0,
            "par_V2G_EVCC_Msg_Timeout_SupportedAppProtocolReq": 2.0,
            "par_V2G_EVCC_Msg_Timeout_SessionSetupReq": 2.0,
            "par_V2G_EVCC_Msg_Timeout_ServiceDiscoveryReq": 2.0,
            "par_V2G_EVCC_Msg_Timeout_ServiceDetailReq": 5.0,
            "par_V2G_EVCC_Msg_Timeout_PaymentServiceSelectionReq": 2.0,
            "par_V2G_EVCC_Msg_Timeout_CertificateInstallationReq": 5.0,
            "par_V2G_EVCC_Msg_Timeout_AuthorizationReq": 2.0,
            "par_V2G_EVCC_Msg_Timeout_ChargeParameterDiscoveryReq": 2.0,
            "par_V2G_EVCC_Msg_Timeout_PowerDeliveryReq": 5.0,
            "par_V2G_EVCC_Msg_Timeout_ChargingStatusReq": 2.0,
            "par_V2G_EVCC_Msg_Timeout_CurrentDemandReq": 0.25,
            "par_V2G_EVCC_Msg_Timeout_WeldingDetectionReq": 2.0,
            "par_V2G_EVCC_Msg_Timeout_SessionStopReq": 2.0,
            "par_V2G_EVCC_Ongoing_Timeout": 60.0,
            "par_V2G_EVCC_CommunicationSetup_Timeout": 20.0,
            "par_V2G_EVCC_CableCheck_Timeout": 40.0,
            "par_V2G_EVCC_PreCharge_Timeout": 7.0,
            "par_EVCC_SDP_Timeout": 0.25,
            "par_CMN_TCP_Connection_Termination_Timeout": 5.0,
        }

        # V2G 消息类型 -> TTCN 映射
        self.message_type_mapping = {
            "SupportedAppProtocol": {
                "req": "SupportedAppProtocolReq",
                "res": "SupportedAppProtocolRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_SupportedAppProtocolReq",
                "section": "E.1.1",
            },
            "SessionSetup": {
                "req": "SessionSetupReq",
                "res": "SessionSetupRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_SessionSetupReq",
                "section": "E.1.2.1",
            },
            "ServiceDiscovery": {
                "req": "ServiceDiscoveryReq",
                "res": "ServiceDiscoveryRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_ServiceDiscoveryReq",
                "section": "E.1.2.2",
            },
            "ServiceDetail": {
                "req": "ServiceDetailReq",
                "res": "ServiceDetailRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_ServiceDetailReq",
                "section": "E.1.2.3",
            },
            "PaymentServiceSelection": {
                "req": "PaymentServiceSelectionReq",
                "res": "PaymentServiceSelectionRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_PaymentServiceSelectionReq",
                "section": "E.1.2.4",
            },
            "PaymentDetails": {
                "req": "PaymentDetailsReq",
                "res": "PaymentDetailsRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_PaymentServiceSelectionReq",
                "section": "E.1.2.5",
            },
            "Authorization": {
                "req": "AuthorizationReq",
                "res": "AuthorizationRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_AuthorizationReq",
                "section": "E.1.2.6",
            },
            "ChargeParameterDiscovery": {
                "req": "ChargeParameterDiscoveryReq",
                "res": "ChargeParameterDiscoveryRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_ChargeParameterDiscoveryReq",
                "section": "E.1.2.7",
            },
            "PowerDelivery": {
                "req": "PowerDeliveryReq",
                "res": "PowerDeliveryRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_PowerDeliveryReq",
                "section": "E.1.2.8",
            },
            "CertificateUpdate": {
                "req": "CertificateUpdateReq",
                "res": "CertificateUpdateRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_CertificateInstallationReq",
                "section": "E.1.2.9",
            },
            "CertificateInstallation": {
                "req": "CertificateInstallationReq",
                "res": "CertificateInstallationRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_CertificateInstallationReq",
                "section": "E.1.2.10",
            },
            "SessionStop": {
                "req": "SessionStopReq",
                "res": "SessionStopRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_SessionStopReq",
                "section": "E.1.2.11",
            },
            "ChargingStatus": {
                "req": "ChargingStatusReq",
                "res": "ChargingStatusRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_ChargingStatusReq",
                "section": "E.1.2.12",
            },
            "MeteringReceipt": {
                "req": "MeteringReceiptReq",
                "res": "MeteringReceiptRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_ChargingStatusReq",
                "section": "E.1.2.13",
            },
            "CableCheck": {
                "req": "CableCheckReq",
                "res": "CableCheckRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_ChargingStatusReq",
                "section": "E.1.2.14",
            },
            "PreCharge": {
                "req": "PreChargeReq",
                "res": "PreChargeRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_ChargingStatusReq",
                "section": "E.1.2.15",
            },
            "CurrentDemand": {
                "req": "CurrentDemandReq",
                "res": "CurrentDemandRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_CurrentDemandReq",
                "section": "E.1.2.16",
            },
            "WeldingDetection": {
                "req": "WeldingDetectionReq",
                "res": "WeldingDetectionRes",
                "timeout_param": "par_V2G_EVCC_Msg_Timeout_WeldingDetectionReq",
                "section": "E.1.2.17",
            },
        }

    def load_documents(self) -> None:
        """加载所有 ISO 15118-4 文档部分"""
        print("加载 ISO 15118-4 文档...")
        contents = []
        for path in self.doc_parts:
            print(f"  加载: {path}")
            with open(path, "r", encoding="utf-8") as f:
                contents.append(f.read())
        self.full_content = "\n".join(contents)
        print(f"  总字符数: {len(self.full_content):,}")

        # OCR 清理: 修复文档转换产生的字符损坏
        print("  执行 OCR 清理...")
        raw_len = len(self.full_content)
        self.full_content = self._clean_ocr_artifacts(self.full_content)
        print(f"  OCR 清理完成, 字符变化: {len(self.full_content) - raw_len:+,}")

        # 合并 Markdown 代码块: 去除 ``` 标记，合并被分割的代码
        print("  合并 Markdown 代码块...")
        self.full_content = self._merge_code_blocks(self.full_content)
        print(f"  合并后总字符数: {len(self.full_content):,}")

    def _clean_ocr_artifacts(self, text: str) -> str:
        """清理 OCR/Markdown 转换产生的损坏 (TODO 1)"""
        # 1. LaTeX 符号替换
        text = re.sub(r'\$\\coloneqq\$', ':=', text)
        text = re.sub(r':\s*\$=\$', ':=', text)      # : $=$ → :=
        text = re.sub(r'\$= =\$', '==', text)
        text = re.sub(r'\$\\equiv\$', '==', text)
        text = re.sub(r'\$\\neq\$', '!=', text)
        text = re.sub(r'\$\\leq\$', '<=', text)
        text = re.sub(r'\$\\geq\$', '>=', text)
        text = re.sub(r'\$\\rightarrow\$', '->', text)
        text = re.sub(r'\$\\leftarrow\$', '<-', text)

        # 2. 中文标点替换
        text = text.replace('\uff1f', '?')   # ？
        text = text.replace('\uff09', ')')   # ）
        text = text.replace('\uff08', '(')   # （
        text = text.replace('\uff0c', ',')   # ，
        text = text.replace('\uff1b', ';')   # ；
        text = text.replace('\u201c', '"')   # "
        text = text.replace('\u201d', '"')   # "
        text = text.replace('\u2018', "'")   # '
        text = text.replace('\u2019', "'")   # '
        text = text.replace('\u3010', '[')   # 【
        text = text.replace('\u3011', ']')   # 】
        text = text.replace('\uff1a', ':')   # ：

        # 3. 转义下划线修复
        text = text.replace('\\_', '_')

        # 4. 已知 OCR 错误修复
        text = text.replace('u1le ', 'module ')  # u1le → module
        text = re.sub(r'pt_V2G美联_', 'pt_V2G_TCP_', text)
        text = re.sub(r'vc苷', 'vc_', text)
        text = re.sub(r'\): = \{', '):= {', text)  # ): = { → ):= {
        text = re.sub(r'\) := \{', '):= {', text)  # normalize spacing

        # 5. 修复名称中的空格（已知模式）
        text = re.sub(r'HAL_61851 Listener', 'HAL_61851_Listener', text)
        text = re.sub(r'HAL 61851', 'HAL_61851', text)
        text = re.sub(r'EVCC Tester', 'EVCCTester', text)
        text = re.sub(r'SECC Tester', 'SECCTester', text)
        text = re.sub(r'a_SECC Unexpected', 'a_SECC_Unexpected', text)
        text = re.sub(r'a_SECC_TCPCConnection_Status Listener',
                       'a_SECC_TCPConnection_Status_Listener', text)
        text = re.sub(r'a_SECC_TCPConnection_Status Listener',
                       'a_SECC_TCPConnection_Status_Listener', text)
        text = re.sub(r'TC_SECC CMN', 'TC_SECC_CMN', text)
        text = re.sub(r'TC_EVCC CMN', 'TC_EVCC_CMN', text)

        # 6. 修复 EVCC/SECC 函数名中丢失的下划线
        text = re.sub(r'\bfEVCC_', 'f_EVCC_', text)
        text = re.sub(r'\bfSECC_', 'f_SECC_', text)
        text = re.sub(r'\baEVCC_', 'a_EVCC_', text)
        text = re.sub(r'\baSECC_', 'a_SECC_', text)

        # 6b. 修复模板名中丢失的下划线
        text = re.sub(r'\bmdEVCC_', 'md_EVCC_', text)
        text = re.sub(r'\bmdSECC_', 'md_SECC_', text)
        text = re.sub(r'\bmdwEVCC_', 'mdw_EVCC_', text)
        text = re.sub(r'\bmdwSECC_', 'mdw_SECC_', text)
        text = re.sub(r'\bmEVCC_', 'm_EVCC_', text)
        text = re.sub(r'\bmSECC_', 'm_SECC_', text)
        text = re.sub(r'\bmwEVCC_', 'mw_EVCC_', text)
        text = re.sub(r'\bmwSECC_', 'mw_SECC_', text)

        # 6c. 修复 ^K (vertical tab, 0x0B) 被当作下划线的情况
        text = text.replace('\x0b', '_')

        # 7. 修复 runs on / system 关键字被 OCR 损坏
        text = re.sub(r'runs\s+011\s+', 'runs on ', text)  # 011 → on
        text = re.sub(r'runson\s+', 'runs on ', text)

        # 8. 修复 ): = { 和类似的 := 分隔问题（template 专用）
        text = re.sub(r'\)\s*:\s*=\s*\{', ') := {', text)
        text = re.sub(r'\)\s*:\s*=\s*\(', ') := (', text)

        return text

    def _merge_code_blocks(self, text: str) -> str:
        """去除 Markdown 代码块标记，合并被分割的代码 (TODO 2)"""
        # 移除所有 ```xxx 开始标记（```txt, ```c, ```javascript 等）
        text = re.sub(r'```\w*\n', '', text)
        # 移除行尾的 ``` 结束标记
        text = re.sub(r'\n```\s*\n', '\n', text)
        # 移除单独一行的 ```
        text = re.sub(r'^```\s*$', '', text, flags=re.MULTILINE)
        return text

    def extract_test_case_descriptions(self) -> List[TestCaseDescription]:
        """提取所有测试用例描述"""
        print("\n提取测试用例描述...")

        # 匹配 TC_SECC 和 TC_EVCC 开头的测试用例 ID
        tc_id_pattern = re.compile(
            r'(TC_(?:SECC|EVCC)_(?:CMN|AC|DC)_VTB_\w+_\d+)'
        )

        # 查找所有测试用例 ID
        all_tc_ids = tc_id_pattern.findall(self.full_content)
        unique_tc_ids = list(dict.fromkeys(all_tc_ids))  # 保持顺序去重

        print(f"  找到 {len(unique_tc_ids)} 个唯一的测试用例 ID")

        # 对每个 TC ID，提取其周围的描述信息
        for tc_id in unique_tc_ids:
            tc_desc = self._extract_single_tc_description(tc_id)
            if tc_desc:
                self.test_case_descriptions.append(tc_desc)

        print(f"  成功提取 {len(self.test_case_descriptions)} 个测试用例描述")
        return self.test_case_descriptions

    def _extract_single_tc_description(self, tc_id: str) -> Optional[TestCaseDescription]:
        """提取单个测试用例描述"""
        # 查找 TC ID 首次出现位置
        idx = self.full_content.find(tc_id)
        if idx == -1:
            return None

        # 获取上下文范围
        context_start = max(0, idx - 200)
        context_end = min(len(self.full_content), idx + 3000)
        context = self.full_content[context_start:context_end]

        # 解析 SUT 类型
        sut_type = "SECC" if "_SECC_" in tc_id else "EVCC"

        # 解析消息类型
        message_type = self._extract_message_type_from_tc_id(tc_id)

        # 解析测试类型
        test_type = "GoodCase" if "_001" in tc_id else "BadCase"

        # 提取需求引用
        req_pattern = re.compile(r'\[V2G2-(\d+)\]')
        requirements = [f"V2G2-{m}" for m in req_pattern.findall(context)]
        requirements = list(dict.fromkeys(requirements))

        # 提取 test objective
        objective = ""
        obj_patterns = [
            re.compile(r'Test\s+(?:objective|purpose)[:\s]*(.+?)(?:\n\n|\|)', re.DOTALL | re.IGNORECASE),
            re.compile(r'Test System (?:executes|shall|sends|checks)(.+?)(?:\n\n|Config)', re.DOTALL),
        ]
        for pattern in obj_patterns:
            match = pattern.search(context)
            if match:
                objective = match.group(1).strip()[:500]
                break

        # 提取 Config Id
        config_match = re.search(r'(CF_\d+_\d+)', context)
        config_id = config_match.group(1) if config_match else ""

        # 提取 PICS selection
        pics_match = re.search(r'(PICS_\w+)', context)
        pics_selection = pics_match.group(0) if pics_match else ""

        # 提取 PreCondition
        pre_match = re.search(r'(f_(?:SECC|EVCC)_\w+_PR_\w+_\d+)', context)
        precondition = pre_match.group(1) if pre_match else ""

        # 提取 Expected behavior (test behavior function)
        behavior_match = re.search(r'(f_(?:SECC|EVCC)_\w+_TB_\w+_\d+)', context)
        expected_behavior = behavior_match.group(1) if behavior_match else ""

        # 提取 Document reference
        doc_ref_match = re.search(r'15118-2:\s*([\d.]+)', context)
        doc_ref = f"ISO:15118-2:2014:IS Section(s): 15118-2:{doc_ref_match.group(1)}" if doc_ref_match else ""

        return TestCaseDescription(
            tc_id=tc_id,
            test_objective=objective,
            document_reference=doc_ref,
            referenced_requirements=requirements,
            config_id=config_id,
            pics_selection=pics_selection,
            pixit_selection="",
            precondition=precondition,
            expected_behavior=expected_behavior,
            sut_type=sut_type,
            message_type=message_type,
            test_type=test_type,
        )

    def _extract_message_type_from_tc_id(self, tc_id: str) -> str:
        """从 TC ID 中提取消息类型"""
        # TC_SECC_CMN_VTB_V2GTPSessionSetup_001 -> V2GTPSessionSetup
        parts = tc_id.split("_VTB_")
        if len(parts) > 1:
            msg_part = parts[1].rsplit("_", 1)[0]
            return msg_part
        return ""

    def extract_ttcn_code_blocks(self) -> List[TTCNCodeBlock]:
        """提取所有 TTCN-3 代码块"""
        print("\n提取 TTCN-3 代码块...")

        # 两阶段提取策略: 先用正则表达式提取，再用行级扫描补充遗漏的定义
        # 阶段 1: 使用正则表达式直接提取（从已合并+清理过的全文中）
        # 提取 testcase 定义
        self._extract_testcases()

        # 提取 function 定义
        self._extract_functions()

        # 提取 template 定义
        self._extract_templates()

        # 提取 altstep 定义
        self._extract_altsteps()

        # 提取 module 定义
        self._extract_modules()

        # 阶段 2: 行级扫描补充提取遗漏的定义
        self._extract_by_line_scan()

        print(f"  总共提取 {len(self.ttcn_code_blocks)} 个 TTCN-3 代码块")
        return self.ttcn_code_blocks

    def _extract_testcases(self) -> None:
        """提取 testcase 代码块 (TODO 3: 放宽正则，支持更多格式)"""
        # 放宽匹配：允许参数列表跨行，runs on / system 之间可有更多空白
        pattern = re.compile(
            r'(testcase\s+TC_\w+\s*\([\s\S]*?\)\s*runs\s+on\s+\w+\s*system\s+\w+\s*\{)',
            re.DOTALL
        )

        seen_names = set()
        for match in pattern.finditer(self.full_content):
            start = match.start()

            # 找到匹配的右大括号
            full_code = self._extract_balanced_braces(self.full_content, start)
            if full_code:
                # 提取 testcase 名称
                name_match = re.search(r'testcase\s+(TC_\w+)', full_code)
                name = name_match.group(1) if name_match else ""

                if name in seen_names:
                    continue
                seen_names.add(name)

                sut_type = "SECC" if "_SECC_" in name else "EVCC"
                msg_type = self._extract_message_type_from_tc_id(name)
                section = self._determine_section(start)

                self.ttcn_code_blocks.append(TTCNCodeBlock(
                    code_type="testcase",
                    name=name,
                    code=full_code,
                    source_section=section,
                    related_message=msg_type,
                    sut_type=sut_type,
                ))

        print(f"  testcase: {sum(1 for b in self.ttcn_code_blocks if b.code_type == 'testcase')}")

    def _extract_functions(self) -> None:
        """提取 function 代码块 (TODO 3: 放宽正则)"""
        # 支持 f_ 前缀以及 fEVCC/fSECC 等 OCR 损坏后已修复的格式
        pattern = re.compile(
            r'(function\s+(f_\w+|f[A-Z]\w+)\s*\([\s\S]*?\)\s*'
            r'(?:runs\s+on\s+\w+)?\s*'
            r'(?:return\s+\w+)?\s*\{)',
            re.DOTALL
        )

        seen_names = set()
        for match in pattern.finditer(self.full_content):
            start = match.start()
            full_code = self._extract_balanced_braces(self.full_content, start)
            if full_code and len(full_code) > 50:
                name_match = re.search(r'function\s+(f_\w+|f[A-Z]\w+)', full_code)
                name = name_match.group(1) if name_match else ""

                # 去重
                if name in seen_names:
                    continue
                seen_names.add(name)

                sut_type = "SECC" if "_SECC_" in name else ("EVCC" if "_EVCC_" in name else "CMN")

                # 判断类型: PR (precondition), TB (test behavior), PO (postcondition)
                func_category = ""
                if "_PR_" in name:
                    func_category = "precondition"
                elif "_TB_" in name:
                    func_category = "test_behavior"
                elif "_PO_" in name:
                    func_category = "postcondition"

                section = self._determine_section(start)

                self.ttcn_code_blocks.append(TTCNCodeBlock(
                    code_type="function",
                    name=name,
                    code=full_code,
                    source_section=section,
                    sut_type=sut_type,
                    category=func_category,
                ))

        print(f"  function: {sum(1 for b in self.ttcn_code_blocks if b.code_type == 'function')}")

    def _extract_templates(self) -> None:
        """提取 template 代码块 (TODO 3: 放宽正则)"""
        # 所有可能的模板名前缀
        name_pattern = r'(md_\w+|mdw_\w+|m_\w+|mw_\w+)'

        # 模式 1: template [modifier] TypeName name(...) := {
        pattern1 = re.compile(
            r'(template\s+(?:\([^)]*\)\s+)?'   # template [modifier]
            r'\w+\s+'                           # TypeName
            + name_pattern + r'\s*'             # template name
            r'\([\s\S]*?\)\s*'                  # parameters (可跨行)
            r':=\s*\{)',                         # := {
            re.DOTALL
        )

        # 模式 2: template [modifier] TypeName name := { (无参数)
        pattern2 = re.compile(
            r'(template\s+(?:\([^)]*\)\s+)?'
            r'\w+\s+'
            + name_pattern + r'\s*'
            r':=\s*\{)',
            re.DOTALL
        )

        # 模式 3: template [modifier] TypeName name(...) := ( (值列表模板, 无大括号)
        pattern3 = re.compile(
            r'(template\s+(?:\([^)]*\)\s+)?'
            r'\w+\s+'
            + name_pattern + r'\s*'
            r'\([\s\S]*?\)\s*'
            r':=\s*\()',
            re.DOTALL
        )

        # 模式 4: template [modifier] TypeName name := ( (值列表, 无参数无大括号)
        pattern4 = re.compile(
            r'(template\s+(?:\([^)]*\)\s+)?'
            r'\w+\s+'
            + name_pattern + r'\s*'
            r':=\s*\()',
            re.DOTALL
        )

        seen_names = set()

        def _add_template(match, start, use_braces=True):
            template_name = match.group(2)
            if template_name in seen_names:
                return
            if use_braces:
                full_code = self._extract_balanced_braces(self.full_content, start)
            else:
                # 对于值列表模板 `:= (...)`, 提取到分号或换行
                paren_start = self.full_content.find('(', start + len(match.group(1)) - 1)
                if paren_start == -1:
                    return
                # 使用括号平衡来提取 (...) 部分
                full_code = self._extract_balanced_parens(self.full_content, start, paren_start)

            if full_code and len(full_code) > 10:
                seen_names.add(template_name)
                section = self._determine_section(start)
                template_category = "receive" if template_name.startswith(("mdw_", "mw_")) else "send"

                self.ttcn_code_blocks.append(TTCNCodeBlock(
                    code_type="template",
                    name=template_name,
                    code=full_code,
                    source_section=section,
                    category=template_category,
                ))

        # 先匹配带参数带大括号的（最严格）
        for match in pattern1.finditer(self.full_content):
            _add_template(match, match.start(), use_braces=True)

        # 再匹配无参数带大括号的
        for match in pattern2.finditer(self.full_content):
            _add_template(match, match.start(), use_braces=True)

        # 匹配值列表模板（带参数）
        for match in pattern3.finditer(self.full_content):
            _add_template(match, match.start(), use_braces=False)

        # 匹配值列表模板（无参数）
        for match in pattern4.finditer(self.full_content):
            _add_template(match, match.start(), use_braces=False)

        print(f"  template: {sum(1 for b in self.ttcn_code_blocks if b.code_type == 'template')}")

    def _extract_balanced_parens(self, text: str, code_start: int, paren_start: int, max_length: int = 5000) -> Optional[str]:
        """提取匹配圆括号的完整代码段（用于值列表模板）"""
        depth = 0
        i = paren_start
        end = min(len(text), paren_start + max_length)

        while i < end:
            ch = text[i]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    # 找到行尾的分号
                    line_end = text.find('\n', i)
                    if line_end == -1:
                        line_end = i + 1
                    return text[code_start:line_end].strip()
            elif ch == '"':
                i += 1
                while i < end and text[i] != '"':
                    if text[i] == '\\':
                        i += 1
                    i += 1
            i += 1

        return text[code_start:end] if depth > 0 else None

    def _extract_altsteps(self) -> None:
        """提取 altstep 代码块 (TODO 3: 放宽正则)"""
        # 支持 a_ 前缀以及 aEVCC/aSECC 等格式，参数可跨行
        pattern = re.compile(
            r'(altstep\s+(a_\w+|a[A-Z]\w+)\s*\([\s\S]*?\)\s*'
            r'(?:runs\s+on\s+\w+)?\s*\{)',
            re.DOTALL
        )

        seen_names = set()
        for match in pattern.finditer(self.full_content):
            start = match.start()
            alt_name = match.group(2)

            if alt_name in seen_names:
                continue

            full_code = self._extract_balanced_braces(self.full_content, start)
            if full_code:
                seen_names.add(alt_name)
                sut_type = "SECC" if "_SECC_" in alt_name else ("EVCC" if "_EVCC_" in alt_name else "CMN")
                section = self._determine_section(start)

                self.ttcn_code_blocks.append(TTCNCodeBlock(
                    code_type="altstep",
                    name=alt_name,
                    code=full_code,
                    source_section=section,
                    sut_type=sut_type,
                ))

        print(f"  altstep: {sum(1 for b in self.ttcn_code_blocks if b.code_type == 'altstep')}")

    def _extract_modules(self) -> None:
        """提取 module 定义"""
        pattern = re.compile(
            r'(module\s+\w+\s*\{[^}]*(?:import\s+from[^}]*)*\})',
            re.DOTALL
        )

        for match in pattern.finditer(self.full_content):
            full_code = match.group(0)
            name_match = re.search(r'module\s+(\w+)', full_code)
            name = name_match.group(1) if name_match else ""

            if name and len(full_code) > 30:
                section = self._determine_section(match.start())
                self.ttcn_code_blocks.append(TTCNCodeBlock(
                    code_type="module",
                    name=name,
                    code=full_code,
                    source_section=section,
                ))

        print(f"  module: {sum(1 for b in self.ttcn_code_blocks if b.code_type == 'module')}")

    def _extract_by_line_scan(self) -> None:
        """通过行级扫描补充提取被正则遗漏的定义 (TODO 5)

        策略：逐行扫描，遇到 TTCN-3 关键字（testcase/function/template/altstep）
        开头的行时，检查该名称是否已提取过，若未提取则使用大括号平衡提取。
        """
        # 收集已提取的名称
        existing_names = {b.name for b in self.ttcn_code_blocks}

        lines = self.full_content.split('\n')
        additional_count = 0

        # 行级关键字识别模式
        line_patterns = [
            # (code_type, regex to match start of definition, name group index)
            ('testcase', re.compile(r'testcase\s+(TC_\w+)'), 1),
            ('function', re.compile(r'function\s+(f_\w+|f[A-Z]\w+)'), 1),
            ('template', re.compile(r'template\s+(?:\([^)]*\)\s+)?\w+\s+(md_\w+|mdw_\w+|m_\w+|mw_\w+)'), 1),
            ('altstep', re.compile(r'altstep\s+(a_\w+|a[A-Z]\w+)'), 1),
        ]

        i = 0
        while i < len(lines):
            line = lines[i]
            for code_type, pattern, name_idx in line_patterns:
                match = pattern.search(line)
                if match:
                    name = match.group(name_idx)
                    if name not in existing_names:
                        # 找到在 full_content 中的位置
                        # 计算此行在 full_content 中的大概偏移
                        line_offset = sum(len(lines[j]) + 1 for j in range(i))
                        pos = self.full_content.find(line.strip(), max(0, line_offset - 100))
                        if pos == -1:
                            pos = self.full_content.find(name)
                        if pos != -1:
                            full_code = self._extract_balanced_braces(self.full_content, pos)
                            if full_code and len(full_code) > 30:
                                existing_names.add(name)
                                section = self._determine_section(pos)
                                sut_type = "SECC" if "_SECC_" in name else (
                                    "EVCC" if "_EVCC_" in name else "CMN")

                                category = ""
                                if code_type == "function":
                                    if "_PR_" in name:
                                        category = "precondition"
                                    elif "_TB_" in name:
                                        category = "test_behavior"
                                    elif "_PO_" in name:
                                        category = "postcondition"
                                elif code_type == "template":
                                    category = "receive" if name.startswith(("mdw_", "mw_")) else "send"

                                msg_type = ""
                                if code_type == "testcase":
                                    msg_type = self._extract_message_type_from_tc_id(name)

                                self.ttcn_code_blocks.append(TTCNCodeBlock(
                                    code_type=code_type,
                                    name=name,
                                    code=full_code,
                                    source_section=section,
                                    related_message=msg_type,
                                    sut_type=sut_type,
                                    category=category,
                                ))
                                additional_count += 1
                    break  # 已匹配到此行，不继续检查其他模式
            i += 1

        if additional_count > 0:
            print(f"  行级扫描补充提取: {additional_count} 个定义")

    def _extract_balanced_braces(self, text: str, start: int, max_length: int = 15000) -> Optional[str]:
        """提取匹配大括号的完整代码块 (TODO 4: max_length 从 5000 增大到 15000)"""
        # 找到第一个 {
        brace_start = text.find("{", start)
        if brace_start == -1:
            return None

        depth = 0
        i = brace_start
        end = min(len(text), start + max_length)

        while i < end:
            ch = text[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
            # 跳过字符串内容
            elif ch == '"':
                i += 1
                while i < end and text[i] != '"':
                    if text[i] == '\\':
                        i += 1
                    i += 1
            i += 1

        # 未找到匹配的结束括号，返回截断的内容
        return text[start:end] if depth > 0 else None

    def _determine_section(self, position: int) -> str:
        """根据位置确定所属章节"""
        # 简化的章节定位
        text_before = self.full_content[max(0, position - 5000):position]

        # 查找最近的 Annex 标记
        annex_match = re.findall(r'Annex\s+([A-Z])', text_before)
        if annex_match:
            return f"Annex {annex_match[-1]}"

        # 查找最近的章节号
        section_match = re.findall(r'#\s+(\d+(?:\.\d+)*)\s+', text_before)
        if section_match:
            return f"Section {section_match[-1]}"

        return "Unknown"

    def build_knowledge_base(self) -> Dict[str, Any]:
        """构建完整的 TTCN-3 知识库"""
        print("\n构建 TTCN-3 知识库...")

        # 按类型组织代码块
        code_by_type = {}
        for block in self.ttcn_code_blocks:
            code_by_type.setdefault(block.code_type, []).append(block.to_dict())

        # 按消息类型组织测试用例描述
        tc_by_message = {}
        for tc in self.test_case_descriptions:
            tc_by_message.setdefault(tc.message_type, []).append(tc.to_dict())

        # 建立 TC description ↔ TTCN code 关联
        tc_code_associations = self._build_tc_code_associations()

        # 按消息类型组织的视图
        by_message_type = self._build_by_message_type_view()

        # 选取高质量 few-shot 样例
        few_shot_examples = self._select_few_shot_examples()

        knowledge_base = {
            "metadata": {
                "source": "ISO 15118-4:2018",
                "description": "TTCN-3 Knowledge Base for ISO 15118-2 Conformance Testing",
                "total_test_cases": len(self.test_case_descriptions),
                "total_code_blocks": len(self.ttcn_code_blocks),
                "total_associations": len(tc_code_associations),
                "total_message_types": len(by_message_type),
            },
            "naming_conventions": self.naming_conventions,
            "component_architecture": self.component_architecture,
            "test_phases": self.test_phases,
            "timer_config": self.timer_config,
            "message_type_mapping": self.message_type_mapping,
            "ttcn_code_by_type": code_by_type,
            "test_case_descriptions_by_message": tc_by_message,
            "tc_code_associations": tc_code_associations,
            "by_message_type": by_message_type,
            "few_shot_examples": few_shot_examples,
        }

        print(f"  知识库统计:")
        print(f"    - 测试用例描述: {len(self.test_case_descriptions)}")
        for code_type, blocks in code_by_type.items():
            print(f"    - {code_type} 代码块: {len(blocks)}")
        print(f"    - TC↔代码关联: {len(tc_code_associations)}")
        print(f"    - 消息类型视图: {len(by_message_type)} 种消息类型")
        print(f"    - Few-shot 样例: {len(few_shot_examples)}")

        return knowledge_base

    def _build_tc_code_associations(self) -> List[Dict[str, Any]]:
        """建立测试用例描述与 TTCN 代码之间的关联 (TODO 6)

        关联规则:
        1. TC description → testcase code: 通过 TC ID 匹配
        2. testcase code → test behavior function: 通过 TB function 名称匹配
        3. test behavior function → templates: 通过模板名称引用匹配
        """
        associations = []

        # 构建代码块索引（按名称）
        code_by_name = {}
        for block in self.ttcn_code_blocks:
            code_by_name[block.name] = block

        # 构建模板名称索引
        template_names = {b.name for b in self.ttcn_code_blocks if b.code_type == "template"}

        for tc_desc in self.test_case_descriptions:
            assoc: Dict[str, Any] = {
                "tc_id": tc_desc.tc_id,
                "message_type": tc_desc.message_type,
                "sut_type": tc_desc.sut_type,
                "test_type": tc_desc.test_type,
                "description": tc_desc.to_dict(),
            }

            # 1. 关联 testcase 代码
            tc_code = code_by_name.get(tc_desc.tc_id)
            if tc_code:
                assoc["testcase_code"] = tc_code.name

            # 2. 关联 test behavior function
            # 从 TC ID 推断 TB function 名称
            # TC_SECC_CMN_VTB_SessionSetup_001 → f_SECC_CMN_TB_VTB_SessionSetup_001
            tb_func_name = tc_desc.tc_id.replace("TC_", "f_").replace("_VTB_", "_TB_VTB_")
            tb_func = code_by_name.get(tb_func_name)
            if tb_func:
                assoc["test_behavior_function"] = tb_func.name

                # 3. 从 TB function 代码中查找引用的模板
                referenced_templates = []
                for tpl_name in template_names:
                    if tpl_name in tb_func.code:
                        referenced_templates.append(tpl_name)
                if referenced_templates:
                    assoc["referenced_templates"] = referenced_templates

            # 关联 precondition function (从描述中获取)
            if tc_desc.precondition:
                pr_func = code_by_name.get(tc_desc.precondition)
                if pr_func:
                    assoc["precondition_function"] = pr_func.name

            # 关联 expected_behavior (从描述中获取)
            if tc_desc.expected_behavior and tc_desc.expected_behavior != tb_func_name:
                eb_func = code_by_name.get(tc_desc.expected_behavior)
                if eb_func:
                    assoc["expected_behavior_function"] = eb_func.name

            associations.append(assoc)

        return associations

    def _build_by_message_type_view(self) -> Dict[str, Dict[str, Any]]:
        """按消息类型组织的知识库视图 (TODO 7)

        结构:
        {
          "SessionSetup": {
            "testcases": [...],
            "test_behavior_functions": [...],
            "precondition_functions": [...],
            "postcondition_functions": [...],
            "send_templates": [...],
            "receive_templates": [...]
          }
        }
        """
        by_message: Dict[str, Dict[str, list]] = {}

        # 从 testcase 代码块中提取消息类型
        for block in self.ttcn_code_blocks:
            msg_type = ""

            if block.code_type == "testcase":
                msg_type = block.related_message or self._extract_message_type_from_tc_id(block.name)

            elif block.code_type == "function":
                # 从函数名中推断消息类型
                # f_SECC_CMN_TB_VTB_SessionSetup_001 → SessionSetup
                msg_type = self._extract_message_type_from_func_name(block.name)

            elif block.code_type == "template":
                # 从模板名中推断消息类型
                # md_CMN_SessionSetupReq_001 → SessionSetup
                # mdw_SECC_SessionSetupRes_001 → SessionSetup
                msg_type = self._extract_message_type_from_template_name(block.name)

            elif block.code_type == "altstep":
                # altstep 通常是通用的，归到 "Common"
                msg_type = "Common"

            if not msg_type:
                msg_type = "Other"

            if msg_type not in by_message:
                by_message[msg_type] = {
                    "testcases": [],
                    "test_behavior_functions": [],
                    "precondition_functions": [],
                    "postcondition_functions": [],
                    "send_templates": [],
                    "receive_templates": [],
                    "altsteps": [],
                    "other": [],
                }

            block_dict = block.to_dict()

            if block.code_type == "testcase":
                by_message[msg_type]["testcases"].append(block_dict)
            elif block.code_type == "function":
                if block.category == "test_behavior":
                    by_message[msg_type]["test_behavior_functions"].append(block_dict)
                elif block.category == "precondition":
                    by_message[msg_type]["precondition_functions"].append(block_dict)
                elif block.category == "postcondition":
                    by_message[msg_type]["postcondition_functions"].append(block_dict)
                else:
                    by_message[msg_type]["other"].append(block_dict)
            elif block.code_type == "template":
                if block.category == "receive" or block.name.startswith("mdw_"):
                    by_message[msg_type]["receive_templates"].append(block_dict)
                else:
                    by_message[msg_type]["send_templates"].append(block_dict)
            elif block.code_type == "altstep":
                by_message[msg_type]["altsteps"].append(block_dict)
            else:
                by_message[msg_type]["other"].append(block_dict)

        # 添加描述到对应的消息类型视图
        for tc_desc in self.test_case_descriptions:
            msg_type = tc_desc.message_type or "Other"
            if msg_type in by_message:
                if "descriptions" not in by_message[msg_type]:
                    by_message[msg_type]["descriptions"] = []
                by_message[msg_type]["descriptions"].append(tc_desc.to_dict())

        # 清理空列表
        for msg_type in by_message:
            by_message[msg_type] = {
                k: v for k, v in by_message[msg_type].items() if v
            }

        return by_message

    def _extract_message_type_from_func_name(self, func_name: str) -> str:
        """从函数名中提取消息类型"""
        # f_SECC_CMN_TB_VTB_SessionSetup_001 → SessionSetup
        # f_SECC_CMN_PR_SessionSetup_001 → SessionSetup
        # f_SECC_CMN_PO_InitConfiguration_001 → InitConfiguration
        parts = func_name.split("_VTB_")
        if len(parts) > 1:
            return parts[1].rsplit("_", 1)[0]

        parts = func_name.split("_PR_")
        if len(parts) > 1:
            return parts[1].rsplit("_", 1)[0]

        parts = func_name.split("_PO_")
        if len(parts) > 1:
            return parts[1].rsplit("_", 1)[0]

        parts = func_name.split("_TB_")
        if len(parts) > 1:
            return parts[1].rsplit("_", 1)[0]

        return ""

    def _extract_message_type_from_template_name(self, template_name: str) -> str:
        """从模板名中提取消息类型"""
        # md_CMN_SessionSetupReq_001 → SessionSetup
        # mdw_SECC_CMN_SessionSetupRes_001 → SessionSetup
        # md_CMN_V2GTP_Message_001 → V2GTP

        # 去除常见的请求/响应后缀来统一消息类型
        name = template_name

        # 移除前缀
        for prefix in ['mdw_SECC_CMN_', 'mdw_EVCC_CMN_', 'mdw_SECC_', 'mdw_EVCC_',
                        'md_CMN_', 'md_SECC_', 'md_EVCC_', 'mdw_CMN_']:
            if name.startswith(prefix):
                name = name[len(prefix):]
                break

        # 移除序号后缀 _001, _002 等
        name = re.sub(r'_\d{3}$', '', name)

        # 移除 Req/Res 后缀
        name = re.sub(r'(Req|Res)$', '', name)

        return name if name else ""

    def _select_few_shot_examples(self) -> List[Dict[str, Any]]:
        """选取高质量 few-shot 样例，每种类型选取代表性的"""
        examples = []

        # 选取代表性 testcase（GoodCase + BadCase 各选1个）
        testcases = [b for b in self.ttcn_code_blocks if b.code_type == "testcase"]
        if testcases:
            # 选取第一个作为 GoodCase 示例
            examples.append({
                "type": "testcase_goodcase",
                "description": "GoodCase testcase structure",
                **testcases[0].to_dict()
            })
            # 选取带 _002 的作为 BadCase 示例
            badcase = next((t for t in testcases if "_002" in t.name), None)
            if badcase:
                examples.append({
                    "type": "testcase_badcase",
                    "description": "BadCase testcase structure",
                    **badcase.to_dict()
                })

        # 选取代表性 test behavior function
        tb_functions = [b for b in self.ttcn_code_blocks
                        if b.code_type == "function" and b.category == "test_behavior"]
        if tb_functions:
            examples.append({
                "type": "test_behavior_function",
                "description": "Test behavior function with alt/setverdict pattern",
                **tb_functions[0].to_dict()
            })

        # 选取代表性 precondition function
        pr_functions = [b for b in self.ttcn_code_blocks
                        if b.code_type == "function" and b.category == "precondition"]
        if pr_functions:
            examples.append({
                "type": "precondition_function",
                "description": "Precondition setup function",
                **pr_functions[0].to_dict()
            })

        # 选取代表性 template
        templates = [b for b in self.ttcn_code_blocks if b.code_type == "template"]
        if templates:
            examples.append({
                "type": "template",
                "description": "TTCN-3 template for message matching",
                **templates[0].to_dict()
            })

        # 选取代表性 altstep
        altsteps = [b for b in self.ttcn_code_blocks if b.code_type == "altstep"]
        if altsteps:
            examples.append({
                "type": "altstep",
                "description": "Altstep for common behavior handling",
                **altsteps[0].to_dict()
            })

        return examples

    def process(self, output_path: str) -> Dict[str, Any]:
        """执行完整处理流程"""
        print("=" * 60)
        print("ISO 15118-4 文档预处理开始")
        print("=" * 60)

        self.load_documents()
        self.extract_test_case_descriptions()
        self.extract_ttcn_code_blocks()
        knowledge_base = self.build_knowledge_base()

        # 保存知识库
        print(f"\n保存知识库: {output_path}")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(knowledge_base, f, ensure_ascii=False, indent=2)

        print(f"\n{'=' * 60}")
        print("ISO 15118-4 预处理完成!")
        print(f"{'=' * 60}")

        return knowledge_base


def main():
    """主函数 - 预处理 ISO 15118-4 文档并构建 TTCN-3 知识库。"""
    script_dir = Path(__file__).parent.resolve()
    # 文档路径相对于 agent/ 目录
    agent_dir = script_dir.parent
    doc_dir = agent_dir / "document"
    output_dir = doc_dir  # 输出到文档目录

    doc_parts = [
        str(doc_dir / "MinerU_markdown_ISO_15118-4-2018_1-500页_2041447572580851712.md"),
        str(doc_dir / "MinerU_markdown_ISO_15118-4-2018_500-1000页_2041447589169328128.md"),
        str(doc_dir / "MinerU_markdown_ISO_15118-4-2018_1000-1468页_2041447601764823040.md"),
    ]
    # 验证文档存在
    missing = [p for p in doc_parts if not Path(p).exists()]
    if missing:
        print(f"警告: 以下文档文件不存在:\n  " + "\n  ".join(missing))

    output_path = str(output_dir / "iso15118_4_ttcn_knowledge_base.json")

    preprocessor = ISO15118_4_Preprocessor(doc_parts)
    knowledge_base = preprocessor.process(output_path)

    print(f"\n输出文件: {output_path}")
    if Path(output_path).exists():
        print(f"文件大小: {Path(output_path).stat().st_size / 1024:.1f} KB")
    else:
        print("文件未生成（可能是文档缺失）")


if __name__ == "__main__":
    main()
