#!/usr/bin/env python3
"""
ISO 15118-2 协议文档预处理脚本 - 增强版
基于 NeTestLLM 论文中的 RFC 树构建方法

增强功能：
1. 更好的章节解析（过滤噪音标题）
2. 消息定义提取（V2G 消息结构）
3. 状态机建模（V2G 通信状态）
4. 时序要求提取
5. 需求编号关联
6. 协议分层映射（OSI 七层）
"""

import re
import json
import os
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple
from pathlib import Path
from enum import Enum


class SectionLevel(Enum):
    """章节层级枚举"""
    CHAPTER = 1      # 一级章节
    SECTION = 2      # 二级章节
    SUBSECTION = 3   # 三级章节
    SUBSUBSECTION = 4  # 四级及以下


class SectionClassification(Enum):
    """章节分类"""
    FUNCTIONAL = "functional"       # 功能性（协议核心）
    NORMATIVE = "normative"         # 规范性
    DESCRIPTIVE = "descriptive"    # 描述性
    APPENDIX = "appendix"          # 附录
    GENERAL = "general"            # 通用


class TestImportance(Enum):
    """测试重要性"""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Section:
    """章节节点"""
    section_number: str = ""
    title: str = ""
    content: str = ""
    level: int = 0
    children: List['Section'] = field(default_factory=list)

    # 分析字段
    summary: str = ""
    classification: str = ""
    test_importance: str = ""
    requirements: List[Dict] = field(default_factory=list)

    # V2G 特定字段
    is_message_def: bool = False
    message_type: Optional[str] = None
    fields: List[Dict] = field(default_factory=list)
    timing_params: List[Dict] = field(default_factory=list)

    # Figure/Table 关联
    figures: List[int] = field(default_factory=list)
    tables: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "section_number": self.section_number,
            "title": self.title,
            "content": self.content,
            "content_length": len(self.content),
            "level": self.level,
            "classification": self.classification,
            "test_importance": self.test_importance,
            "requirements": self.requirements,
            "is_message_def": self.is_message_def,
            "message_type": self.message_type,
            "fields": self.fields,
            "timing_params": self.timing_params,
            "figures": self.figures,
            "tables": self.tables,
            "children": [child.to_dict() for child in self.children]
        }


class ISO15118Preprocessor:
    """ISO 15118-2 文档预处理器 - 增强版"""
    
    def __init__(self, markdown_path: str, image_dir: str = "", table_dir: str = ""):
        self.markdown_path = Path(markdown_path)
        self.raw_content: str = ""
        self.lines: List[str] = []

        self.metadata: Dict[str, str] = {}
        self.root_sections: List[Section] = []
        self.toc_entries: List[Dict] = []

        # Figure/Table 索引
        self.figure_index: Dict[int, Dict] = {}  # {fig_num: {title, image_paths[], line_num}}
        self.table_index: Dict[int, Dict] = {}   # {tbl_num: {title, file_path, line_num}}
        self.image_dir = image_dir or str(Path(markdown_path).parent / "image")
        self.table_dir = table_dir or str(Path(markdown_path).parent / "table")

        # 协议分层映射（OSI 参考模型）
        self.osi_layers = {
            '7': 'Application Layer',
            '8': 'Application Layer (Messages)',
            '7.10': 'Application Layer (SECC Discovery)',
        }

        # V2G 通信状态（用于状态机建模）
        self.v2g_states = [
            'V2GCommunicationSession', 'V2GInitiated', 'V2GCommunicationSetup',
            'SessionSetup', 'ServiceDiscovery', 'PaymentServiceSelection',
            'ContractCertificate', 'ChargeParameterDiscovery', 'PowerDelivery',
            'PowerDeliveryActive', 'SessionStop', 'V2GCommunicationTeardown'
        ]
        
    def load_document(self) -> None:
        """加载文档"""
        print(f"加载文档: {self.markdown_path}")
        with open(self.markdown_path, 'r', encoding='utf-8') as f:
            self.raw_content = f.read()
        self.lines = self.raw_content.split('\n')
        print(f"文档总行数: {len(self.lines)}")

    def build_figure_index(self) -> None:
        """构建 Figure 索引，支持多页 figure"""
        print("\n构建 Figure 索引...")
        figure_pattern = re.compile(r'^#*\s*Figure\s+(\d+)\s*[—–-]\s*(.+)$')

        for i, line in enumerate(self.lines):
            m = figure_pattern.match(line.strip())
            if not m:
                continue
            fig_num = int(m.group(1))
            fig_title = m.group(2).strip()

            # 查找对应的图片文件（支持多页: figure_N.png, figure_N_1.png, figure_N_2.png）
            image_paths = []
            single = os.path.join(self.image_dir, f"figure_{fig_num}.png")
            if os.path.exists(single):
                image_paths.append(f"image/figure_{fig_num}.png")
            # 多页
            for page in range(1, 10):
                multi = os.path.join(self.image_dir, f"figure_{fig_num}_{page}.png")
                if os.path.exists(multi):
                    image_paths.append(f"image/figure_{fig_num}_{page}.png")

            self.figure_index[fig_num] = {
                "title": fig_title,
                "image_paths": image_paths,
                "line_num": i + 1
            }

        print(f"  Figure 索引: {len(self.figure_index)} 条")

    def build_table_index(self) -> None:
        """构建 Table 索引"""
        print("\n构建 Table 索引...")
        table_pattern = re.compile(r'^Table\s+(\d+)\s*[—–-]\s*(.+)$')

        for i, line in enumerate(self.lines):
            m = table_pattern.match(line.strip())
            if not m:
                continue
            tbl_num = int(m.group(1))
            tbl_title = m.group(2).strip()

            file_path = f"table/table_{tbl_num}.md"
            abs_path = os.path.join(self.table_dir, f"table_{tbl_num}.md")

            self.table_index[tbl_num] = {
                "title": tbl_title,
                "file_path": file_path if os.path.exists(abs_path) else None,
                "line_num": i + 1
            }

        print(f"  Table 索引: {len(self.table_index)} 条")

    @staticmethod
    def _find_figure_refs(text: str) -> List[int]:
        """从文本中提取引用的 Figure 编号"""
        return sorted(set(int(m) for m in re.findall(r'Figure\s+(\d+)', text)))

    @staticmethod
    def _find_table_refs(text: str) -> List[int]:
        """从文本中提取引用的 Table 编号"""
        return sorted(set(int(m) for m in re.findall(r'Table\s+(\d+)', text)))

    def extract_metadata(self) -> None:
        """提取元数据"""
        print("\n提取元数据...")
        
        # 提取标题
        title_patterns = [
            r'#\s+(Road vehicles[^\n]+)',
            r'Part\s+2:\s*([^\n]+)',
        ]
        for pattern in title_patterns:
            match = re.search(pattern, self.raw_content)
            if match:
                self.metadata['title'] = match.group(1).strip()
                break
        
        # 提取日期
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', self.raw_content)
        if date_match:
            self.metadata['date'] = date_match.group(1)
        
        # 版权年份
        copyright_match = re.search(r'©\s*ISO\s+(\d{4})', self.raw_content)
        if copyright_match:
            self.metadata['copyright_year'] = copyright_match.group(1)
        
        # 文档版本
        edition_match = re.search(r'(First|Second|Third)\s+edition', self.raw_content, re.IGNORECASE)
        if edition_match:
            self.metadata['edition'] = edition_match.group(1)
        
        self.metadata['filename'] = self.markdown_path.name
        print(f"元数据: {json.dumps(self.metadata, indent=2)}")
    
    def extract_toc(self) -> List[Dict]:
        """提取目录结构 - 构建嵌套树"""
        print("\n提取目录...")
        
        toc_entries = []
        
        for line in self.raw_content.split('\n'):
            if '# Contents' in line or '# Table of Contents' in line:
                continue
            
            if line.strip().startswith('#'):
                continue
            
            match = re.match(
                r'^\s*([\d.]+)\s+([A-Za-z][^.]+?)\s+\.{2,}\s*(\d+)?',
                line
            )
            if match:
                section_num = match.group(1).strip()
                title = match.group(2).strip().rstrip('.')
                page = match.group(3) if match.group(3) else ""
                
                toc_entries.append({
                    'section_number': section_num,
                    'title': title,
                    'page': page
                })
        
        toc_tree = self._build_toc_tree(toc_entries)
        
        self.toc_entries = toc_tree
        print(f"提取到 {len(toc_entries)} 个目录项")
        return toc_tree
    
    def _build_toc_tree(self, entries: List[Dict]) -> List[Dict]:
        """将扁平目录构建为嵌套树"""
        if not entries:
            return []
        
        def sort_key(e):
            parts = e['section_number'].split('.')
            return [int(p) if p.isdigit() else p for p in parts]
        
        entries_sorted = sorted(entries, key=sort_key)
        
        root = []
        stack = [(0, root)]
        
        for entry in entries_sorted:
            section_num = entry['section_number']
            parts = section_num.split('.')
            level = len(parts)
            
            node = {
                'section_number': section_num,
                'title': entry['title'],
                'page': entry.get('page', ''),
                'children': []
            }
            
            while stack and stack[-1][0] >= level:
                stack.pop()
            
            if stack:
                stack[-1][1].append(node)
            else:
                root.append(node)
            
            stack.append((level, node['children']))
        
        return root
    
    def is_valid_section_title(self, title: str) -> bool:
        """判断是否为有效的章节标题"""
        # 过滤噪音标题
        skip_patterns = [
            r'^Road vehicles',
            r'^Part \d+:',
            r'^Véhicules routiers',
            r'^Table K\.\d+',
            r'^Figure \d+',
            r'^V2G message example \d+',
            r'^NOTE',
            r'^Key$',
            r'^\d+\.\d+$',  # 单独的数字标题
            r'^\[V2G2-\d+\]',  # 需求引用
        ]
        
        title_stripped = title.strip()
        
        # 跳过空标题或过短标题
        if len(title_stripped) < 3:
            return False
        
        # 跳过包含特定模式的标题
        for pattern in skip_patterns:
            if re.match(pattern, title_stripped):
                return False
        
        return True
    
    def parse_sections(self) -> List[Section]:
        """解析文档结构，构建章节树"""
        print("\n解析章节结构...")
        
        sections = []
        section_stack: List[Tuple[Section, int]] = []  # (section, level)
        
        # 章节标题模式
        section_pattern = re.compile(r'^(#{1,4})\s+(?:(\d+(?:\.\d+)*)\s+)?([^\n]+)$')
        
        i = 0
        while i < len(self.lines):
            line = self.lines[i]
            match = section_pattern.match(line)
            
            if match:
                level = len(match.group(1))
                section_num = match.group(2) if match.group(2) else ""
                title = match.group(3).strip()
                
                # 验证标题有效性
                if not self.is_valid_section_title(title):
                    i += 1
                    continue
                
                # 创建新章节
                new_section = Section(
                    section_number=section_num,
                    title=title,
                    level=level,
                    content=""
                )
                
                # 收集内容（直到下一个同级或更高级标题）
                content_start = i + 1
                content_lines = []
                
                j = content_start
                while j < len(self.lines):
                    next_line = self.lines[j]
                    next_match = section_pattern.match(next_line)
                    
                    if next_match:
                        next_level = len(next_match.group(1))
                        if next_level <= level:
                            break
                    
                    content_lines.append(next_line)
                    
                    # 限制内容长度以节省内存
                    if len('\n'.join(content_lines)) > 50000:
                        break
                    j += 1
                
                new_section.content = '\n'.join(content_lines)
                
                # 提取需求编号
                new_section.requirements = self._extract_requirements(new_section.content, section_num)

                # 提取 V2G 消息定义
                self._extract_message_def(new_section)

                # 提取时间参数
                new_section.timing_params = self._extract_timing(new_section.content)

                # 提取章节中引用的 Figure/Table
                new_section.figures = self._find_figure_refs(new_section.content)
                new_section.tables = self._find_table_refs(new_section.content)
                
                # 构建树结构
                while section_stack and section_stack[-1][1] >= level:
                    section_stack.pop()
                
                if not section_stack:
                    sections.append(new_section)
                else:
                    section_stack[-1][0].children.append(new_section)
                
                section_stack.append((new_section, level))
                
                i = j
            else:
                i += 1
        
        self.root_sections = sections
        
        # 统计
        total = self._count_sections(sections)
        print(f"解析到 {len(sections)} 个一级章节，共 {total} 个章节")
        
        # 打印主要章节
        main_sections = [s for s in sections if s.level == 1 and s.section_number]
        for sec in main_sections[:10]:
            child_count = self._count_sections(sec.children)
            print(f"  - {sec.section_number or 'N/A'} {sec.title} ({child_count} 子节)")
        
        return sections
    
    def _extract_requirements(self, text: str, section_num: str = "") -> List[Dict]:
        """提取需求编号和完整内容，并关联引用的 figure/table"""
        requirements = []

        v2g_pattern = r'\[(V2G2-\d+)\]'

        for match in re.finditer(v2g_pattern, text):
            req_id = match.group(1)

            # 提取需求上下文：围绕需求引用的句子
            start = max(0, match.start() - 100)
            end = min(len(text), match.end() + 100)
            context = text[start:end].strip()

            # 尝试获取完整句子
            line_start = text.rfind('\n', 0, match.start()) + 1
            line_end = text.find('\n', match.end())
            if line_end == -1:
                line_end = len(text)

            sentence = text[line_start:line_end].strip()
            sentence = re.sub(r'\s+', ' ', sentence)

            # 扫描需求上下文（前后 500 字符）中的 Figure/Table 引用
            ctx_start = max(0, match.start() - 500)
            ctx_end = min(len(text), match.end() + 500)
            ctx_window = text[ctx_start:ctx_end]

            requirements.append({
                "requirement_id": req_id,
                "content": sentence if sentence else context,
                "section": section_num,
                "referenced_figures": self._find_figure_refs(ctx_window),
                "referenced_tables": self._find_table_refs(ctx_window),
            })

        # 去重
        seen = set()
        unique_reqs = []
        for req in requirements:
            if req["requirement_id"] not in seen:
                seen.add(req["requirement_id"])
                unique_reqs.append(req)

        return unique_reqs
    
    def _extract_message_def(self, section: Section) -> None:
        """提取 V2G 消息定义 - 增强版"""
        content_lower = section.content.lower()
        title_lower = section.title.lower()

        # 检查是否为消息定义章节（扩展匹配逻辑）
        is_msg = any(kw in title_lower for kw in ['req', 'res', 'message', 'type'])
        is_schema = '<v2g' in content_lower or 'xsd:' in content_lower
        is_schema_diagram = 'schema diagram' in title_lower
        is_semantics = 'semantics and type definition' in title_lower

        if (is_msg and is_schema) or is_schema_diagram or is_semantics:
            section.is_message_def = True

            # 提取消息类型 - 多种模式
            msg_type = None

            # 模式1: Schema Diagram 标题 (如 "Schema Diagram – SessionSetupReq")
            schema_match = re.search(r'Schema\s+Diagram\s*[—–-]\s*(\w+)', section.title)
            if schema_match:
                msg_type = schema_match.group(1)

            # 模式2: Semantics and type definition 标题
            sem_match = re.search(r'Semantics.*?for\s+(\w+(?:Req|Res))', section.title, re.IGNORECASE)
            if sem_match:
                msg_type = sem_match.group(1)

            # 模式3: XML 标签中的消息类型
            if not msg_type:
                msg_pattern = re.compile(r'<(\w+:)?([A-Za-z]+(?:Req|Res|Type))')
                msg_matches = msg_pattern.findall(section.content)
                if msg_matches:
                    msg_type = msg_matches[0][1]

            # 模式4: 标题中直接包含消息名
            if not msg_type:
                title_msg = re.search(r'((?:Session|Service|Payment|Certificate|Charge|Power|Cable|Welding|Metering|Current|Pre)\w*(?:Req|Res))', section.title)
                if title_msg:
                    msg_type = title_msg.group(1)

            section.message_type = msg_type

            # 提取字段
            field_pattern = re.compile(r'<(\w+:)?(\w+)>')
            fields = field_pattern.findall(section.content)
            section.fields = [{'xmlns': f[0].rstrip(':') if f[0] else '',
                              'name': f[1]} for f in fields[:20]]
    
    def _extract_timing(self, text: str) -> List[Dict]:
        """提取时间参数 - 增强版，支持 V2G 特定命名"""
        timings = []

        # V2G 特定命名模式（par_V2G_*, V2G_EVCC_Msg_*, V2G_SECC_Msg_*）
        v2g_patterns = [
            (r'(par_V2G_\w+)', 'v2g_param'),
            (r'(V2G_EVCC_Msg_\w+)', 'evcc_timer'),
            (r'(V2G_SECC_Msg_\w+)', 'secc_timer'),
            (r'(V2G_SECC_CommunicationSetup_\w+)', 'secc_timer'),
            (r'(V2G_EVCC_CommunicationSetup_\w+)', 'evcc_timer'),
            (r'(Sequence_\w*[Tt]ime\w*)', 'sequence_timer'),
        ]

        for pattern, param_type in v2g_patterns:
            for match in re.finditer(pattern, text):
                param_name = match.group(1).strip()
                # 尝试提取关联的数值
                after = text[match.end():match.end() + 100]
                val_match = re.search(r'[=:]\s*([\d.]+)\s*(s|ms|seconds?|milliseconds?)?', after)
                value = val_match.group(1) if val_match else ""
                unit = val_match.group(2) if val_match and val_match.group(2) else "s"
                timings.append({
                    'parameter': param_name,
                    'value': value,
                    'unit': unit.lower() if unit else "s",
                    'type': param_type
                })

        # 通用时间参数模式
        generic_patterns = [
            (r'(T_?\w*(?:timeout|delay|duration)?):\s*([\d.]+)\s*(s|ms|seconds?|milliseconds?)?', 'timer'),
            (r'(t_?\w*(?:timeout|delay|duration)?):\s*([\d.]+)\s*(s|ms|seconds?|milliseconds?)?', 'timer'),
            (r'(?:shall|must|should)\s+(?:be\s+)?(?:completed|executed|sent|received)\s+(?:within|in|after)\s+([\d.]+)\s*(s|ms|seconds?|milliseconds?)?', 'duration'),
            (r'(?:maximum|minimum)\s+(?:time|duration|timeout)\s+(?:of\s+)?([\d.]+)\s*(s|ms|seconds?|milliseconds?)?', 'limit'),
        ]

        for pattern, param_type in generic_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                param_name = match.group(1).strip()
                value = match.group(2)
                unit = match.group(3) if match.group(3) else "s"
                unit = unit.lower()
                if unit in ['seconds', 'second', 's'] and value and float(value) < 1:
                    unit = 'ms'
                    value = str(float(value) * 1000)
                timings.append({
                    'parameter': param_name,
                    'value': value,
                    'unit': unit,
                    'type': param_type
                })

        # 去重
        seen = set()
        unique = []
        for t in timings:
            key = t['parameter']
            if key not in seen:
                seen.add(key)
                unique.append(t)

        return unique[:50]
    
    def classify_sections(self) -> None:
        """章节分类与重要性评估"""
        print("\n章节分类与重要性评估...")
        
        # 分类关键词
        functional_kw = [
            'message', 'protocol', 'layer', 'service', 'request', 'response',
            'session', 'handshake', 'parameter', 'security', 'certificate',
            'error', 'timing', 'sequence', 'encoding', 'schema', 'req', 'res'
        ]
        
        normative_kw = [
            'requirement', 'shall', 'must', 'normative', 'annex c',
            'annex f', 'annex h'
        ]
        
        appendix_kw = [
            'annex a', 'annex b', 'annex d', 'annex e', 'annex g',
            'annex j', 'annex k', 'example', 'bibliography'
        ]
        
        # 重要性映射
        importance_map = {
            '7': 'high',      # Basic requirements - 核心协议要求
            '8': 'high',      # Application Layer messages - 消息定义
            '6': 'medium',    # Document overview
            '5': 'medium',    # Conventions
            '3': 'low',       # Terms and definitions
            '4': 'low',       # Symbols
            '2': 'low',       # Normative references
        }
        
        for section in self.root_sections:
            self._classify_recursive(section, {
                'functional': functional_kw,
                'normative': normative_kw,
                'appendix': appendix_kw
            }, importance_map)
        
        # 统计
        stats = self._collect_stats(self.root_sections)
        # 转换 set 为 list 以便 JSON 序列化
        stats_for_print = {k: (list(v) if isinstance(v, set) else v) for k, v in stats.items()}
        print(f"分类统计: {json.dumps(stats_for_print, indent=2)}")
    
    def _classify_recursive(self, section: Section, keyword_rules: Dict, 
                           importance_map: Dict) -> None:
        """递归分类"""
        title_lower = section.title.lower()
        
        # 分类
        if any(kw in title_lower for kw in keyword_rules['appendix']):
            section.classification = 'appendix'
        elif any(kw in title_lower for kw in keyword_rules['normative']):
            section.classification = 'normative'
        elif any(kw in title_lower for kw in keyword_rules['functional']):
            section.classification = 'functional'
        else:
            section.classification = 'general'
        
        # 重要性
        if section.section_number:
            first_num = section.section_number.split('.')[0]
            if first_num.isdigit():
                section.test_importance = importance_map.get(first_num, 'low')
        
        # 递归
        for child in section.children:
            self._classify_recursive(child, keyword_rules, importance_map)
    
    def _count_sections(self, sections: List[Section]) -> int:
        """统计章节总数"""
        count = len(sections)
        for sec in sections:
            count += self._count_sections(sec.children)
        return count
    
    def _collect_stats(self, sections: List[Section]) -> Dict:
        """收集统计信息"""
        def traverse(secs):
            total = 0
            func = 0
            norm = 0
            app = 0
            gen = 0
            high = 0
            reqs = set()
            
            for sec in secs:
                total += 1
                if sec.classification == 'functional':
                    func += 1
                elif sec.classification == 'normative':
                    norm += 1
                elif sec.classification == 'appendix':
                    app += 1
                else:
                    gen += 1
                
                if sec.test_importance == 'high':
                    high += 1
                
                reqs.update([r["requirement_id"] for r in sec.requirements])
                
                child_stats = traverse(sec.children)
                total += child_stats['total']
                func += child_stats['functional']
                norm += child_stats['normative']
                app += child_stats['appendix']
                gen += child_stats['general']
                high += child_stats['high_importance']
                reqs.update(child_stats['requirements'])
            
            return {
                'total': total,
                'functional': func,
                'normative': norm,
                'appendix': app,
                'general': gen,
                'high_importance': high,
                'requirements': reqs
            }
        
        return traverse(sections)
    
    def build_protocol_summary(self) -> Dict:
        """构建协议摘要"""
        print("\n构建协议摘要...")
        
        summary = {
            'protocol_name': 'ISO 15118-2',
            'protocol_description': 'V2G Communication Interface - Network and Application Protocol',
            'osi_layers': {
                '7_Application': 'V2G Application Layer Messages (Section 8)',
                '7_Presentation': 'XML/EXI Encoding (Section 7.9)',
                '7_Transfer': 'V2G Transfer Protocol (V2GTP) (Section 7.8)',
                '7_Transport_TCP': 'TCP (Section 7.7.1)',
                '7_Transport_TLS': 'TLS (Section 7.7.3)',
                '7_Transport_UDP': 'UDP (Section 7.7.2)',
                '3_Network': 'IPv6 (Section 7.6)',
                '2_DataLink': 'Data Link Layer (Section 7.5)',
                '1_Physical': 'Physical Layer (ISO 15118-3)',
            },
            'key_concepts': {
                'session_management': 'V2G Communication Session handling',
                'security': 'TLS, XML Signatures, Certificate management',
                'identification_modes': ['Plug-and-Charge (PnC)', 'External Identification Means (EIM)'],
                'charging_modes': ['AC Charging', 'DC Charging'],
            },
            'main_sections': []
        }
        
        # 提取主要章节
        for sec in self.root_sections:
            if sec.level == 1 and sec.section_number in ['7', '8']:
                summary['main_sections'].append({
                    'section': sec.section_number,
                    'title': sec.title,
                    'importance': sec.test_importance,
                    'requirements_count': len(sec.requirements)
                })
        
        return summary
    
    def generate_json_output(self, output_path: str) -> None:
        """生成完整的 JSON 输出"""
        print(f"\n生成 JSON 输出: {output_path}")
        
        # 收集所有需求
        all_requirements = []
        seen_req_ids = set()
        def collect_reqs(sections):
            for sec in sections:
                for req in sec.requirements:
                    if req["requirement_id"] not in seen_req_ids:
                        seen_req_ids.add(req["requirement_id"])
                        all_requirements.append(req)
                collect_reqs(sec.children)
        collect_reqs(self.root_sections)
        all_requirements.sort(key=lambda x: x["requirement_id"])
        
        # 转换 stats 中的 set 为 list
        stats = self._collect_stats(self.root_sections)
        stats_serializable = {k: (list(v) if isinstance(v, set) else v) for k, v in stats.items()}
        
        output = {
            "document_metadata": self.metadata,
            "document_type": "ISO 15118-2:2014",
            "protocol": "V2G (Vehicle-to-Grid) Communication Interface",

            "protocol_summary": self.build_protocol_summary(),

            "figure_index": {str(k): v for k, v in sorted(self.figure_index.items())},

            "table_index": {str(k): v for k, v in sorted(self.table_index.items())},

            "table_of_contents": self.toc_entries,

            "section_tree": {
                "root": "ISO15118_2",
                "total_sections": self._count_sections(self.root_sections),
                "sections": [s.to_dict() for s in self.root_sections]
            },

            "statistics": stats_serializable,

            "all_requirements": all_requirements,

            "v2g_messages": self._extract_all_messages(),

            "v2g_timing_params": self._extract_all_timing(),
        }
        
        # 输出到文件
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        
        print(f"JSON 输出完成")
        print(f"  - 总章节数: {output['statistics']['total']}")
        print(f"  - 功能性章节: {output['statistics'].get('functional', 0)}")
        print(f"  - 重要性高章节: {output['statistics']['high_importance']}")
        print(f"  - 需求编号数: {len(all_requirements)}")
        print(f"  - Figure 索引: {len(self.figure_index)} 条")
        print(f"  - Table 索引: {len(self.table_index)} 条")
        print(f"  - V2G 消息定义: {len(output['v2g_messages'])} 条")
        print(f"  - 时序参数: {len(output['v2g_timing_params'])} 条")
    
    def _extract_all_messages(self) -> List[Dict]:
        """提取所有消息定义"""
        messages = []
        
        def traverse(sections):
            for sec in sections:
                if sec.is_message_def:
                    messages.append({
                        'section': sec.section_number,
                        'title': sec.title,
                        'message_type': sec.message_type,
                        'fields': sec.fields,
                        'requirements': [r["requirement_id"] for r in sec.requirements]
                    })
                traverse(sec.children)
        
        traverse(self.root_sections)
        return messages[:50]  # 限制数量
    
    def _extract_all_timing(self) -> List[Dict]:
        """提取所有时间参数"""
        timings = []
        
        def traverse(sections):
            for sec in sections:
                for tp in sec.timing_params:
                    tp['section'] = sec.section_number
                    timings.append(tp)
                traverse(sec.children)
        
        traverse(self.root_sections)
        
        # 去重
        seen = set()
        unique_timings = []
        for t in timings:
            key = (t['parameter'], t['value'])
            if key not in seen:
                seen.add(key)
                unique_timings.append(t)
        
        return unique_timings[:50]
    
    def process(self, output_path: str) -> Dict:
        """执行完整处理流程"""
        print("="*60)
        print("ISO 15118-2 文档预处理开始")
        print("="*60)

        self.load_document()
        self.extract_metadata()
        self.build_figure_index()
        self.build_table_index()
        self.extract_toc()
        self.parse_sections()
        self.classify_sections()
        self.generate_json_output(output_path)

        print("\n" + "="*60)
        print("预处理完成！")
        print("="*60)

        return {
            'metadata': self.metadata,
            'sections': self.root_sections,
            'toc_entries': self.toc_entries
        }


def main():
    """主函数"""
    input_file = "/data/cdy/code/v2g2_082/iso15118/agent/document/MinerU_markdown_ISO_15118-2-2014_2034243645514502144.md"
    output_file = "/data/cdy/code/v2g2_082/iso15118/agent/document/iso15118_2_hierarchical_tree.json"

    preprocessor = ISO15118Preprocessor(input_file)
    preprocessor.process(output_file)

    print(f"\n输出文件: {output_file}")


if __name__ == "__main__":
    main()
