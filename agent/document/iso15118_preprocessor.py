#!/usr/bin/env python3
"""
ISO 15118-2 协议文档预处理脚本
基于 NeTestLLM 论文中的 RFC 树构建方法

功能：
1. 清理文档（移除页眉页脚、版权信息）
2. 提取元数据（标准号、标题、摘要、目录）
3. 构建层次化章节树
4. 生成结构化 JSON 输出
"""

import re
import json
import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
from pathlib import Path


@dataclass
class Section:
    """章节节点"""
    section_number: str = ""
    title: str = ""
    content: str = ""
    level: int = 0  # 1=章, 2=节, 3=子节
    children: List['Section'] = field(default_factory=list)
    parent: Optional['Section'] = None
    
    # 协议分析相关字段
    summary: str = ""
    classification: str = ""  # functional, descriptive, appendix, normative
    test_importance: str = ""  # high, medium, low
    requirements: List[str] = field(default_factory=list)  # [V2G2-xxx] 格式的需求
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，排除循环引用"""
        result = {
            "section_number": self.section_number,
            "title": self.title,
            "content_preview": self.content[:500] + "..." if len(self.content) > 500 else self.content,
            "content_length": len(self.content),
            "level": self.level,
            "classification": self.classification,
            "test_importance": self.test_importance,
            "requirements": self.requirements,
            "children": [child.to_dict() for child in self.children]
        }
        return result


class ISO15118Preprocessor:
    """ISO 15118-2 文档预处理器"""
    
    def __init__(self, markdown_path: str):
        self.markdown_path = Path(markdown_path)
        self.raw_content: str = ""
        self.lines: List[str] = []
        
        # 元数据
        self.metadata: Dict[str, str] = {}
        
        # 章节树
        self.root_sections: List[Section] = []
        
        # 目录结构（用于映射）
        self.toc_entries: List[Dict] = []
        
    def load_document(self) -> None:
        """加载文档"""
        print(f"加载文档: {self.markdown_path}")
        with open(self.markdown_path, 'r', encoding='utf-8') as f:
            self.raw_content = f.read()
        self.lines = self.raw_content.split('\n')
        print(f"文档总行数: {len(self.lines)}")
    
    def extract_metadata(self) -> None:
        """提取元数据"""
        print("\n提取元数据...")
        
        # 提取标准编号
        title_match = re.search(r'#\s*(Road vehicles[^\n]+)', self.raw_content)
        if title_match:
            self.metadata['title'] = title_match.group(1).strip()
        
        # 提取标准号
        part_match = re.search(r'Part\s+2:\s*([^\n]+)', self.raw_content)
        if part_match:
            self.metadata['part'] = part_match.group(1).strip()
        
        # 提取日期
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', self.raw_content)
        if date_match:
            self.metadata['date'] = date_match.group(1)
        
        # 提取版权信息
        copyright_match = re.search(r'©\s*ISO\s+(\d{4})', self.raw_content)
        if copyright_match:
            self.metadata['copyright_year'] = copyright_match.group(1)
        
        self.metadata['filename'] = self.markdown_path.name
        print(f"元数据: {self.metadata}")
    
    def clean_content(self) -> str:
        """清理文档内容，移除无关信息"""
        cleaned_lines = []
        skip_patterns = [
            r'^!\[[^\]]*\]\(https?://',  # 外部图片链接
            r'^```$',  # 空代码块标记
            r'^#\s*COPYRIGHT',  # 版权页
            r'^ISO copyright office',  # ISO 版权办公室
        ]
        
        skip_until_patterns = [
            (r'^#\s*Foreword', r'^#\s*(?:Introduction|1\s+Scope)'),
            (r'^#\s*Bibliography', None),  # 参考文献之后全部跳过
        ]
        
        skip = False
        skip_end_pattern = None
        
        for line in self.lines:
            # 检查是否进入跳过区域
            for start_pattern, end_pattern in skip_patterns:
                if re.match(start_pattern, line):
                    skip = True
                    skip_end_pattern = end_pattern
                    break
            
            if skip:
                # 检查是否离开跳过区域
                if skip_end_pattern and re.match(skip_end_pattern, line):
                    skip = False
                    skip_end_pattern = None
                elif skip_end_pattern is None and line.startswith('# '):
                    # 无结束模式的跳过，遇到新章节标题停止
                    if not any(re.match(s, line) for s, _ in skip_patterns if s.startswith('^#')):
                        skip = False
                continue
            
            # 移除尾部空行和多余空格
            if line.strip():
                cleaned_lines.append(line.rstrip())
        
        return '\n'.join(cleaned_lines)
    
    def extract_toc(self) -> List[Dict]:
        """提取目录结构"""
        print("\n提取目录...")
        
        toc_start = False
        toc_entries = []
        
        for line in self.lines:
            # 找到目录开始
            if '# Contents' in line or '# Table of Contents' in line:
                toc_start = True
                continue
            
            # 找到目录结束（通常是 Foreword 或 Introduction）
            if toc_start and re.match(r'^#\s+(?:Foreword|Introduction|1\s+Scope)', line):
                break
            
            if toc_start:
                # 解析目录项
                # 格式: "1 Scope .... ." 或 "5.1 Definition of OSI based services .... 8"
                match = re.match(r'^\s*([\d.]+)\s+([A-Za-z][^.]+?)\s+\.{3,}\s*\.?\s*(\d+)?', line)
                if match:
                    section_num = match.group(1).strip()
                    title = match.group(2).strip().rstrip('.')
                    page = match.group(3) if match.group(3) else ""
                    
                    # 判断层级
                    if '.' in section_num:
                        parts = section_num.split('.')
                        level = len(parts)
                    else:
                        level = 1
                    
                    toc_entries.append({
                        'section_number': section_num,
                        'title': title,
                        'level': level,
                        'page': page
                    })
        
        self.toc_entries = toc_entries
        print(f"提取到 {len(toc_entries)} 个目录项")
        return toc_entries
    
    def parse_sections(self) -> List[Section]:
        """解析文档结构，构建章节树"""
        print("\n解析章节结构...")
        
        sections = []
        current_section: Optional[Section] = None
        current_level = 0
        
        # 章节标题模式: # 1 Scope, # 7 Basic requirements, ## 7.1 General information
        section_pattern = re.compile(r'^(#{1,4})\s+(?:(\d+(?:\.\d+)*)\s+)?([^\n]+)$')
        
        for i, line in enumerate(self.lines):
            match = section_pattern.match(line)
            
            if match:
                level = len(match.group(1))  # # 的数量决定层级
                section_num = match.group(2) if match.group(2) else ""
                title = match.group(3).strip()
                
                # 跳过非内容章节
                skip_titles = ['Foreword', 'Introduction', 'Bibliography', 
                              'COPYRIGHT PROTECTED DOCUMENT', 'Contents']
                if any(skip in title for skip in skip_titles):
                    continue
                
                # 创建新章节
                new_section = Section(
                    section_number=section_num,
                    title=title,
                    level=level,
                    content=""  # 内容稍后填充
                )
                
                # 收集需求编号
                new_section.requirements = self._extract_requirements(
                    '\n'.join(self.lines[i:min(i+100, len(self.lines))])
                )
                
                # 根据层级决定插入位置
                if level == 1:
                    sections.append(new_section)
                    current_section = new_section
                elif level == 2 and sections:
                    sections[-1].children.append(new_section)
                    new_section.parent = sections[-1]
                    current_section = new_section
                elif level >= 3:
                    # 找到最近的父节点
                    if sections and sections[-1].children:
                        sections[-1].children[-1].children.append(new_section)
                        new_section.parent = sections[-1].children[-1]
                        current_section = new_section
                
                current_level = level
            elif current_section:
                # 累加内容
                current_section.content += line + '\n'
        
        self.root_sections = sections
        print(f"解析到 {len(sections)} 个一级章节")
        for sec in sections:
            print(f"  - {sec.section_number} {sec.title} ({len(sec.children)} 子节)")
        
        return sections
    
    def _extract_requirements(self, text: str) -> List[str]:
        """提取需求编号，如 [V2G2-001]"""
        # 匹配 [V2G2-数字] 格式
        pattern = r'\[V2G2-(\d+)\]'
        matches = re.findall(pattern, text)
        return [f"V2G2-{m}" for m in matches]
    
    def classify_sections(self) -> None:
        """基于规则对章节进行分类和重要性评估"""
        print("\n章节分类与重要性评估...")
        
        # 分类规则
        functional_keywords = [
            'message', 'protocol', 'layer', 'service', 'request', 'response',
            'session', 'handshake', 'parameter', 'security', 'certificate',
            'error', 'timing', 'sequence', 'encoding', 'schema'
        ]
        
        descriptive_keywords = [
            'general', 'overview', 'introduction', 'scope', 'definition',
            'reference', 'convention', 'symbol', 'abbreviation', 'term'
        ]
        
        normative_keywords = [
            'requirement', 'shall', 'must', 'normative', 'annex c',
            'annex f', 'annex h', 'annex i'  # Normative annexes
        ]
        
        appendix_keywords = [
            'annex a', 'annex b', 'annex d', 'annex e', 'annex g',
            'annex j', 'annex k', 'example', 'bibliography'
        ]
        
        # 重要性规则
        important_sections = {
            '7': 'high',      # Basic requirements
            '8': 'high',      # Application Layer messages
            '6': 'medium',    # Document overview
        }
        
        for section in self.root_sections:
            self._classify_section(section, {
                'functional': functional_keywords,
                'descriptive': descriptive_keywords,
                'normative': normative_keywords,
                'appendix': appendix_keywords
            }, important_sections)
    
    def _classify_section(self, section: Section, keyword_rules: Dict, 
                          important_rules: Dict) -> None:
        """递归分类单个章节"""
        title_lower = section.title.lower()
        
        # 分类
        if any(kw in title_lower for kw in keyword_rules['appendix']):
            section.classification = 'appendix'
        elif any(kw in title_lower for kw in keyword_rules['normative']):
            section.classification = 'normative'
        elif any(kw in title_lower for kw in keyword_rules['functional']):
            section.classification = 'functional'
        elif any(kw in title_lower for kw in keyword_rules['descriptive']):
            section.classification = 'descriptive'
        else:
            section.classification = 'general'
        
        # 重要性
        if section.section_number:
            first_num = section.section_number.split('.')[0]
            section.test_importance = important_rules.get(first_num, 'low')
        
        # 递归处理子章节
        for child in section.children:
            self._classify_section(child, keyword_rules, important_rules)
    
    def extract_message_definitions(self) -> List[Dict]:
        """提取消息定义（协议特定建模）"""
        print("\n提取消息定义...")
        
        messages = []
        
        # 查找消息定义章节
        msg_pattern = re.compile(
            r'^\d+\.\d+\.\d+\s+(?:Message|Request|Response)\s+(?:Definition|Type)?:?\s*(.+?)(?=\n\n|\n#)',
            re.MULTILINE | re.IGNORECASE
        )
        
        # V2G消息模式
        v2g_msg_pattern = re.compile(r'<v2g[^:>]+:([A-Za-z]+)>', re.IGNORECASE)
        
        for match in msg_pattern.finditer(self.raw_content):
            msg_type = match.group(1).strip()
            context = self.raw_content[match.start():match.start()+2000]
            
            # 查找相关字段
            fields = v2g_msg_pattern.findall(context)
            
            messages.append({
                'name': msg_type,
                'context_preview': context[:200],
                'fields': list(set(fields))[:10]  # 限制字段数量
            })
        
        print(f"提取到 {len(messages)} 个消息定义")
        return messages
    
    def extract_timing_requirements(self) -> List[Dict]:
        """提取时间要求"""
        print("\n提取时间要求...")
        
        timings = []
        
        # 时间模式: T_xxx, timeout, delay
        timing_pattern = re.compile(
            r'([TM]_?\w*(?:Timeout|Delay|Duration|Time|timing)[\w\s]*?):\s*([\d.]+)\s*(?:ms|s|seconds?|milliseconds?)?',
            re.IGNORECASE
        )
        
        for match in timing_pattern.finditer(self.raw_content):
            param_name = match.group(1).strip()
            value = match.group(2)
            unit = match.group(3) if match.group(3) else "s"
            
            timings.append({
                'parameter': param_name,
                'value': value,
                'unit': unit
            })
        
        print(f"提取到 {len(timings)} 个时间参数")
        return timings
    
    def generate_json_output(self, output_path: str) -> None:
        """生成完整的 JSON 输出"""
        print(f"\n生成 JSON 输出: {output_path}")
        
        output = {
            "document_metadata": self.metadata,
            "document_type": "ISO 15118-2:2014",
            "protocol": "V2G (Vehicle-to-Grid) Communication Interface",
            "description": "Road vehicles — Vehicle-to-Grid Communication Interface — Part 2: Network and application protocol requirements",
            
            "table_of_contents": self.toc_entries,
            
            "section_tree": {
                "root": "ISO15118_2",
                "total_sections": self._count_sections(self.root_sections),
                "sections": [s.to_dict() for s in self.root_sections]
            },
            
            "statistics": {
                "total_lines": len(self.lines),
                "total_sections": self._count_sections(self.root_sections),
                "functional_sections": self._count_by_classification(self.root_sections, 'functional'),
                "normative_sections": self._count_by_classification(self.root_sections, 'normative'),
                "total_requirements": len(self._collect_all_requirements(self.root_sections))
            },
            
            "extracted_data": {
                "messages": self.extract_message_definitions(),
                "timing_requirements": self.extract_timing_requirements()
            }
        }
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        
        print(f"JSON 输出完成，共 {output['statistics']['total_sections']} 个章节")
    
    def _count_sections(self, sections: List[Section]) -> int:
        """递归统计章节总数"""
        count = len(sections)
        for sec in sections:
            count += self._count_sections(sec.children)
        return count
    
    def _count_by_classification(self, sections: List[Section], 
                                 classification: str) -> int:
        """统计特定分类的章节数"""
        count = sum(1 for s in sections if s.classification == classification)
        for sec in sections:
            count += self._count_by_classification(sec.children, classification)
        return count
    
    def _collect_all_requirements(self, sections: List[Section]) -> List[str]:
        """收集所有需求编号"""
        reqs = []
        for sec in sections:
            reqs.extend(sec.requirements)
            reqs.extend(self._collect_all_requirements(sec.children))
        return list(set(reqs))
    
    def process(self, output_path: str) -> Dict:
        """执行完整处理流程"""
        print("="*60)
        print("ISO 15118-2 文档预处理开始")
        print("="*60)
        
        # 1. 加载文档
        self.load_document()
        
        # 2. 提取元数据
        self.extract_metadata()
        
        # 3. 提取目录
        self.extract_toc()
        
        # 4. 解析章节结构
        self.parse_sections()
        
        # 5. 章节分类与重要性评估
        self.classify_sections()
        
        # 6. 生成 JSON 输出
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
    # 输入输出路径
    input_file = "/data/cdy/code/v2g2_082/document/MinerU_markdown_ISO_15118-2-2014_2034243645514502144.md"
    output_file = "/data/cdy/code/v2g2_082/document/iso15118_2_hierarchical_tree.json"
    
    # 创建预处理器并执行
    preprocessor = ISO15118Preprocessor(input_file)
    result = preprocessor.process(output_file)
    
    print(f"\n输出文件: {output_file}")
    print(f"处理完成!")


if __name__ == "__main__":
    main()
