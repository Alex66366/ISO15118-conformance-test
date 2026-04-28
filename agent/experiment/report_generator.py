#!/usr/bin/env python3
"""
评估报告生成器
生成多格式的对比评估报告，包括文本、Markdown、HTML 格式。

功能特性：
- 支持单个测试用例报告
- 支持批量测试套件报告
- 生成趋势分析（如果有历史数据）
- 可视化指标图表
- 自定义模板

用法:
    python report_generator.py --results ./experiment/comparison_results/TC_XXX.json
    python report_generator.py --batch --results-dir ./experiment/comparison_results
    python report_generator.py --template detailed --output ./report.html
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


# ============================================================
# 数据结构
# ============================================================
class ReportFormat(Enum):
    TEXT = "text"
    MARKDOWN = "markdown"
    HTML = "html"
    JSON = "json"


class ReportType(Enum):
    SINGLE = "single"
    BATCH = "batch"
    TREND = "trend"


@dataclass
class MetricScore:
    """指标得分"""
    name: str
    score: float
    weight: float
    status: str  # pass, warning, fail


@dataclass
class TestCaseReport:
    """单个测试用例报告"""
    tc_id: str
    status: str  # pass, warning, fail
    overall_similarity: float
    metrics: Dict[str, MetricScore] = field(default_factory=dict)
    issues: List[Dict] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    generated_path: str = ""
    reference_path: str = ""


@dataclass
class BatchReport:
    """批量测试报告"""
    suite_name: str
    timestamp: str
    total_cases: int
    passed: int
    failed: int
    warnings: int
    case_reports: List[TestCaseReport] = field(default_factory=list)
    aggregate_metrics: Dict[str, float] = field(default_factory=dict)
    overall_score: float = 0.0


# ============================================================
# 报告生成器基类
# ============================================================
class ReportGenerator:
    """报告生成器基类"""
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.template_dir = Path(__file__).parent / "templates"
    
    def generate(self, data: Any, output_path: Path, fmt: ReportFormat = ReportFormat.TEXT) -> None:
        """生成报告"""
        raise NotImplementedError


# ============================================================
# 文本报告生成器
# ============================================================
class TextReportGenerator(ReportGenerator):
    """文本格式报告生成器"""
    
    def generate(self, data: Any, output_path: Path, fmt: ReportFormat = ReportFormat.TEXT) -> None:
        """生成文本报告"""
        if isinstance(data, TestCaseReport):
            content = self._format_single(data)
        elif isinstance(data, BatchReport):
            content = self._format_batch(data)
        else:
            content = str(data)
        
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
    
    def _format_single(self, report: TestCaseReport) -> str:
        """格式化单个测试用例报告"""
        lines = []
        
        # 标题
        status_icon = {"pass": "✓", "warning": "⚠", "fail": "✗"}.get(report.status, "?")
        lines.append("=" * 70)
        lines.append(f"测试用例评估报告: {report.tc_id}")
        lines.append(f"状态: [{report.status.upper()}] {status_icon}")
        lines.append("=" * 70)
        
        # 总体相似度
        lines.append(f"\n综合相似度: {report.overall_similarity:.2%}")
        
        # 指标得分
        lines.append("\n--- 指标得分 ---")
        for name, metric in report.metrics.items():
            bar = "█" * int(metric.score * 20) + "░" * (20 - int(metric.score * 20))
            status_icon = {"pass": "✓", "warning": "⚠", "fail": "✗"}.get(metric.status, "?")
            lines.append(f"  {name:30s}: [{bar}] {metric.score:.1%} {status_icon}")
        
        # 统计信息
        if report.stats:
            lines.append("\n--- 统计信息 ---")
            for key, value in report.stats.items():
                lines.append(f"  {key:30s}: {value}")
        
        # 问题列表
        if report.issues:
            lines.append(f"\n--- 问题列表 ({len(report.issues)}) ---")
            for i, issue in enumerate(report.issues, 1):
                severity = issue.get("severity", "info")
                sev_icon = {"critical": "✗", "warning": "⚠", "info": "ℹ"}.get(severity, "•")
                lines.append(f"  {i}. [{severity.upper()}] {sev_icon} {issue.get('description', '')}")
        
        # 文件路径
        lines.append("\n--- 文件信息 ---")
        if report.generated_path:
            lines.append(f"  生成的代码: {report.generated_path}")
        if report.reference_path:
            lines.append(f"  参考代码:   {report.reference_path}")
        
        lines.append("\n" + "=" * 70)
        
        return "\n".join(lines)
    
    def _format_batch(self, report: BatchReport) -> str:
        """格式化批量测试报告"""
        lines = []
        
        # 标题
        lines.append("=" * 80)
        lines.append(f"批量测试评估报告: {report.suite_name}")
        lines.append(f"时间: {report.timestamp}")
        lines.append("=" * 80)
        
        # 总体统计
        lines.append("\n--- 总体统计 ---")
        lines.append(f"  总计测试用例: {report.total_cases}")
        lines.append(f"  通过: {report.passed} ({report.passed/report.total_cases*100:.1f}%)")
        lines.append(f"  警告: {report.warnings} ({report.warnings/report.total_cases*100:.1f}%)")
        lines.append(f"  失败: {report.failed} ({report.failed/report.total_cases*100:.1f}%)")
        lines.append(f"  综合得分: {report.overall_score:.2%}")
        
        # 聚合指标
        if report.aggregate_metrics:
            lines.append("\n--- 聚合指标得分 ---")
            for name, score in report.aggregate_metrics.items():
                bar = "█" * int(score * 20) + "░" * (20 - int(score * 20))
                lines.append(f"  {name:30s}: [{bar}] {score:.1%}")
        
        # 每个测试用例摘要
        lines.append("\n--- 测试用例摘要 ---")
        lines.append(f"{'TC ID':<50} {'状态':<10} {'相似度':<10}")
        lines.append("-" * 70)
        for case in report.case_reports:
            status_icon = {"pass": "✓", "warning": "⚠", "fail": "✗"}.get(case.status, "?")
            lines.append(f"{case.tc_id:<50} {case.status:<10} {case.overall_similarity:.1%} {status_icon}")
        
        # 问题汇总
        all_issues = []
        for case in report.case_reports:
            for issue in case.issues:
                if issue.get("severity") == "critical":
                    all_issues.append((case.tc_id, issue))
        
        if all_issues:
            lines.append(f"\n--- 严重问题汇总 ({len(all_issues)}) ---")
            for tc_id, issue in all_issues:
                lines.append(f"  [{tc_id}] {issue.get('description', '')}")
        
        lines.append("\n" + "=" * 80)
        
        return "\n".join(lines)


# ============================================================
# Markdown 报告生成器
# ============================================================
class MarkdownReportGenerator(ReportGenerator):
    """Markdown 格式报告生成器"""
    
    def generate(self, data: Any, output_path: Path, fmt: ReportFormat = ReportFormat.MARKDOWN) -> None:
        """生成 Markdown 报告"""
        if isinstance(data, TestCaseReport):
            content = self._format_single(data)
        elif isinstance(data, BatchReport):
            content = self._format_batch(data)
        else:
            content = str(data)
        
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
    
    def _format_single(self, report: TestCaseReport) -> str:
        """格式化单个测试用例 Markdown 报告"""
        lines = []
        
        # 标题
        status_emoji = {"pass": "✅", "warning": "⚠️", "fail": "❌"}.get(report.status, "❓")
        lines.append(f"# TTCN-3 代码评估报告")
        lines.append("")
        lines.append(f"**测试用例**: `{report.tc_id}`")
        lines.append("")
        lines.append(f"**状态**: {status_emoji} **{report.status.upper()}**")
        lines.append("")
        lines.append(f"**综合相似度**: {report.overall_similarity:.2%}")
        lines.append("")
        
        # 指标得分
        lines.append("## 指标得分")
        lines.append("")
        lines.append("| 指标 | 得分 | 状态 |")
        lines.append("|------|------|------|")
        for name, metric in report.metrics.items():
            status_icon = {"pass": "✅", "warning": "⚠️", "fail": "❌"}.get(metric.status, "➖")
            lines.append(f"| {name} | {metric.score:.1%} | {status_icon} |")
        lines.append("")
        
        # 统计信息
        if report.stats:
            lines.append("## 统计信息")
            lines.append("")
            lines.append("| 项目 | 值 |")
            lines.append("|------|-----|")
            for key, value in report.stats.items():
                lines.append(f"| {key} | {value} |")
            lines.append("")
        
        # 问题列表
        if report.issues:
            lines.append("## 问题列表")
            lines.append("")
            for i, issue in enumerate(report.issues, 1):
                severity = issue.get("severity", "info")
                sev_icon = {"critical": "❌", "warning": "⚠️", "info": "ℹ️"}.get(severity, "➖")
                lines.append(f"{i}. {sev_icon} **{severity.upper()}**: {issue.get('description', '')}")
            lines.append("")
        
        # 文件路径
        lines.append("## 文件信息")
        lines.append("")
        if report.generated_path:
            lines.append(f"- **生成的代码**: `{report.generated_path}`")
        if report.reference_path:
            lines.append(f"- **参考代码**: `{report.reference_path}`")
        lines.append("")
        
        # 元数据
        lines.append("---")
        lines.append(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
        
        return "\n".join(lines)
    
    def _format_batch(self, report: BatchReport) -> str:
        """格式化批量测试 Markdown 报告"""
        lines = []
        
        # 标题
        lines.append(f"# 批量测试评估报告")
        lines.append("")
        lines.append(f"**测试套件**: {report.suite_name}")
        lines.append(f"**时间**: {report.timestamp}")
        lines.append(f"**总计**: {report.total_cases} 个测试用例")
        lines.append("")
        
        # 总体统计
        pass_rate = report.passed / report.total_cases * 100 if report.total_cases > 0 else 0
        lines.append("## 总体统计")
        lines.append("")
        lines.append("```")
        lines.append(f"通过: {report.passed}/{report.total_cases} ({pass_rate:.1f}%)")
        lines.append(f"警告: {report.warnings}/{report.total_cases}")
        lines.append(f"失败: {report.failed}/{report.total_cases}")
        lines.append(f"综合得分: {report.overall_score:.2%}")
        lines.append("```")
        lines.append("")
        
        # 聚合指标
        if report.aggregate_metrics:
            lines.append("## 聚合指标得分")
            lines.append("")
            lines.append("| 指标 | 得分 |")
            lines.append("|------|------|")
            for name, score in report.aggregate_metrics.items():
                lines.append(f"| {name} | {score:.1%} |")
            lines.append("")
        
        # 测试用例摘要表格
        lines.append("## 测试用例摘要")
        lines.append("")
        lines.append("| TC ID | 状态 | 相似度 | 问题数 |")
        lines.append("|-------|------|--------|--------|")
        for case in report.case_reports:
            issue_count = len([i for i in case.issues if i.get("severity") == "critical"])
            status_icon = {"pass": "✅", "warning": "⚠️", "fail": "❌"}.get(case.status, "➖")
            lines.append(f"| {case.tc_id} | {status_icon} {case.status} | {case.overall_similarity:.1%} | {issue_count} |")
        lines.append("")
        
        # 失败用例详情
        failed_cases = [c for c in report.case_reports if c.status == "fail"]
        if failed_cases:
            lines.append("## 失败用例详情")
            lines.append("")
            for case in failed_cases:
                lines.append(f"### {case.tc_id}")
                lines.append("")
                lines.append(f"- 相似度: {case.overall_similarity:.2%}")
                lines.append(f"- 问题数: {len(case.issues)}")
                for issue in case.issues[:5]:
                    lines.append(f"  - {issue.get('description', '')}")
                lines.append("")
        
        # 元数据
        lines.append("---")
        lines.append(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
        
        return "\n".join(lines)


# ============================================================
# HTML 报告生成器
# ============================================================
class HTMLReportGenerator(ReportGenerator):
    """HTML 格式报告生成器"""
    
    def generate(self, data: Any, output_path: Path, fmt: ReportFormat = ReportFormat.HTML) -> None:
        """生成 HTML 报告"""
        if isinstance(data, TestCaseReport):
            content = self._format_single(data)
        elif isinstance(data, BatchReport):
            content = self._format_batch(data)
        else:
            content = f"<pre>{data}</pre>"
        
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
    
    def _get_css(self) -> str:
        """获取 CSS 样式"""
        return """
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
                line-height: 1.6;
                max-width: 1200px;
                margin: 0 auto;
                padding: 20px;
                background-color: #f5f5f5;
            }
            .container {
                background: white;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                padding: 30px;
            }
            h1 {
                color: #333;
                border-bottom: 3px solid #4CAF50;
                padding-bottom: 10px;
            }
            h2 {
                color: #555;
                margin-top: 30px;
                border-left: 4px solid #4CAF50;
                padding-left: 10px;
            }
            .status {
                display: inline-block;
                padding: 5px 15px;
                border-radius: 4px;
                font-weight: bold;
            }
            .status.pass { background: #4CAF50; color: white; }
            .status.warning { background: #FF9800; color: white; }
            .status.fail { background: #f44336; color: white; }
            .metric {
                margin: 10px 0;
            }
            .metric-bar {
                background: #e0e0e0;
                border-radius: 4px;
                height: 24px;
                position: relative;
            }
            .metric-fill {
                height: 100%;
                border-radius: 4px;
                transition: width 0.3s ease;
            }
            .metric-fill.high { background: linear-gradient(90deg, #4CAF50, #8BC34A); }
            .metric-fill.medium { background: linear-gradient(90deg, #FF9800, #FFC107); }
            .metric-fill.low { background: linear-gradient(90deg, #f44336, #FF5722); }
            .metric-label {
                position: absolute;
                left: 10px;
                top: 50%;
                transform: translateY(-50%);
                font-weight: 500;
                color: #333;
            }
            .metric-value {
                position: absolute;
                right: 10px;
                top: 50%;
                transform: translateY(-50%);
                font-weight: bold;
                color: #333;
            }
            table {
                width: 100%;
                border-collapse: collapse;
                margin: 20px 0;
            }
            th, td {
                padding: 12px;
                text-align: left;
                border-bottom: 1px solid #ddd;
            }
            th {
                background: #4CAF50;
                color: white;
            }
            tr:hover { background: #f5f5f5; }
            .issue {
                padding: 10px;
                margin: 5px 0;
                border-radius: 4px;
                border-left: 4px solid;
            }
            .issue.critical { background: #FFEBEE; border-color: #f44336; }
            .issue.warning { background: #FFF3E0; border-color: #FF9800; }
            .issue.info { background: #E3F2FD; border-color: #2196F3; }
            .footer {
                margin-top: 30px;
                padding-top: 20px;
                border-top: 1px solid #ddd;
                color: #666;
                font-size: 0.9em;
            }
            .summary-cards {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 20px;
                margin: 20px 0;
            }
            .card {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 20px;
                border-radius: 8px;
                text-align: center;
            }
            .card-value { font-size: 2em; font-weight: bold; }
            .card-label { opacity: 0.9; }
            .card.pass { background: linear-gradient(135deg, #4CAF50 0%, #8BC34A 100%); }
            .card.warning { background: linear-gradient(135deg, #FF9800 0%, #FFC107 100%); }
            .card.fail { background: linear-gradient(135deg, #f44336 0%, #FF5722 100%); }
        </style>
        """
    
    def _format_single(self, report: TestCaseReport) -> str:
        """格式化单个测试用例 HTML 报告"""
        status_color = {"pass": "pass", "warning": "warning", "fail": "fail"}.get(report.status, "")
        
        html = [
            "<!DOCTYPE html>",
            "<html>",
            "<head>",
            "<meta charset='UTF-8'>",
            "<title>TTCN-3 代码评估报告</title>",
            self._get_css(),
            "</head>",
            "<body>",
            "<div class='container'>",
            f"<h1>TTCN-3 代码评估报告</h1>",
            f"<p><strong>测试用例</strong>: <code>{report.tc_id}</code></p>",
            f"<p><strong>状态</strong>: <span class='status {status_color}'>{report.status.upper()}</span></p>",
            f"<p><strong>综合相似度</strong>: {report.overall_similarity:.2%}</p>",
        ]
        
        # 指标得分
        html.append("<h2>指标得分</h2>")
        html.append("<div class='metrics'>")
        for name, metric in report.metrics.items():
            if metric.score >= 0.8:
                fill_class = "high"
            elif metric.score >= 0.5:
                fill_class = "medium"
            else:
                fill_class = "low"
            html.append(f"""
            <div class='metric'>
                <span class='metric-label'>{name}</span>
                <div class='metric-bar'>
                    <div class='metric-fill {fill_class}' style='width: {metric.score * 100}%'></div>
                    <span class='metric-value'>{metric.score:.1%}</span>
                </div>
            </div>
            """)
        html.append("</div>")
        
        # 统计信息
        if report.stats:
            html.append("<h2>统计信息</h2>")
            html.append("<table>")
            for key, value in report.stats.items():
                html.append(f"<tr><td>{key}</td><td>{value}</td></tr>")
            html.append("</table>")
        
        # 问题列表
        if report.issues:
            html.append("<h2>问题列表</h2>")
            for issue in report.issues:
                severity = issue.get("severity", "info")
                html.append(f"<div class='issue {severity}'>")
                html.append(f"<strong>[{severity.upper()}]</strong> {issue.get('description', '')}")
                html.append("</div>")
        
        # 页脚
        html.extend([
            "<div class='footer'>",
            f"<p>报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>",
            "</div>",
            "</div>",
            "</body>",
            "</html>",
        ])
        
        return "\n".join(html)
    
    def _format_batch(self, report: BatchReport) -> str:
        """格式化批量测试 HTML 报告"""
        pass_rate = report.passed / report.total_cases * 100 if report.total_cases > 0 else 0
        
        html = [
            "<!DOCTYPE html>",
            "<html>",
            "<head>",
            "<meta charset='UTF-8'>",
            "<title>批量测试评估报告</title>",
            self._get_css(),
            "</head>",
            "<body>",
            "<div class='container'>",
            f"<h1>批量测试评估报告</h1>",
            f"<p><strong>测试套件</strong>: {report.suite_name}</p>",
            f"<p><strong>时间</strong>: {report.timestamp}</p>",
        ]
        
        # 摘要卡片
        html.append("<div class='summary-cards'>")
        html.append(f"<div class='card'>")
        html.append(f"<div class='card-value'>{report.total_cases}</div>")
        html.append(f"<div class='card-label'>总计用例</div>")
        html.append(f"</div>")
        html.append(f"<div class='card pass'>")
        html.append(f"<div class='card-value'>{report.passed}</div>")
        html.append(f"<div class='card-label'>通过</div>")
        html.append(f"</div>")
        html.append(f"<div class='card warning'>")
        html.append(f"<div class='card-value'>{report.warnings}</div>")
        html.append(f"<div class='card-label'>警告</div>")
        html.append(f"</div>")
        html.append(f"<div class='card fail'>")
        html.append(f"<div class='card-value'>{report.failed}</div>")
        html.append(f"<div class='card-label'>失败</div>")
        html.append(f"</div>")
        html.append("</div>")
        
        # 测试用例表格
        html.append("<h2>测试用例摘要</h2>")
        html.append("<table>")
        html.append("<tr><th>TC ID</th><th>状态</th><th>相似度</th><th>问题数</th></tr>")
        for case in report.case_reports:
            status_color = {"pass": "pass", "warning": "warning", "fail": "fail"}.get(case.status, "")
            issue_count = len([i for i in case.issues if i.get("severity") == "critical"])
            html.append(f"<tr>")
            html.append(f"<td><code>{case.tc_id}</code></td>")
            html.append(f"<td><span class='status {status_color}'>{case.status.upper()}</span></td>")
            html.append(f"<td>{case.overall_similarity:.1%}</td>")
            html.append(f"<td>{issue_count}</td>")
            html.append(f"</tr>")
        html.append("</table>")
        
        # 页脚
        html.extend([
            "<div class='footer'>",
            f"<p>报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>",
            "</div>",
            "</div>",
            "</body>",
            "</html>",
        ])
        
        return "\n".join(html)


# ============================================================
# 报告生成工厂
# ============================================================
class ReportGeneratorFactory:
    """报告生成器工厂"""
    
    _generators = {
        ReportFormat.TEXT: TextReportGenerator,
        ReportFormat.MARKDOWN: MarkdownReportGenerator,
        ReportFormat.HTML: HTMLReportGenerator,
    }
    
    @classmethod
    def create(cls, fmt: ReportFormat) -> ReportGenerator:
        """创建报告生成器"""
        generator_class = cls._generators.get(fmt)
        if not generator_class:
            raise ValueError(f"不支持的报告格式: {fmt}")
        return generator_class()


# ============================================================
# 报告数据转换器
# ============================================================
class ReportDataConverter:
    """将原始数据转换为报告数据格式"""
    
    @staticmethod
    def from_comparison_result(result: Dict) -> TestCaseReport:
        """从对比结果创建测试用例报告"""
        # 转换指标
        metrics = {}
        raw_metrics = result.get("metrics", {})
        weights = {
            "structural_similarity": 0.3,
            "syntax_correctness": 0.25,
            "semantic_coverage": 0.25,
            "naming_convention": 0.1,
            "completeness": 0.1,
        }
        for name, score in raw_metrics.items():
            weight = weights.get(name, 0.1)
            status = "pass" if score >= 0.7 else "warning" if score >= 0.4 else "fail"
            metrics[name] = MetricScore(
                name=name,
                score=score,
                weight=weight,
                status=status
            )
        
        return TestCaseReport(
            tc_id=result.get("tc_id", ""),
            status=result.get("status", "warning"),
            overall_similarity=result.get("overall_similarity", 0.0),
            metrics=metrics,
            issues=result.get("issues", []),
            stats=result.get("stats", {}),
            generated_path=result.get("generated_path", ""),
            reference_path=result.get("reference_path", ""),
        )
    
    @staticmethod
    def from_batch_results(results: List[Dict], suite_name: str) -> BatchReport:
        """从批量对比结果创建批量报告"""
        case_reports = [ReportDataConverter.from_comparison_result(r) for r in results]
        
        passed = sum(1 for c in case_reports if c.status == "pass")
        failed = sum(1 for c in case_reports if c.status == "fail")
        warnings = sum(1 for c in case_reports if c.status == "warning")
        
        # 聚合指标
        aggregate_metrics = {}
        metric_names = ["structural_similarity", "syntax_correctness", "semantic_coverage", 
                       "naming_convention", "completeness"]
        
        for metric_name in metric_names:
            scores = [c.metrics.get(metric_name, MetricScore(metric_name, 0, 0, "fail")).score 
                     for c in case_reports]
            if scores:
                aggregate_metrics[metric_name] = sum(scores) / len(scores)
        
        # 计算总体得分
        overall_score = sum(aggregate_metrics.get(m, 0) * w 
                           for m, w in [("structural_similarity", 0.3),
                                        ("syntax_correctness", 0.25),
                                        ("semantic_coverage", 0.25),
                                        ("naming_convention", 0.1),
                                        ("completeness", 0.1)])
        
        return BatchReport(
            suite_name=suite_name,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            total_cases=len(results),
            passed=passed,
            failed=failed,
            warnings=warnings,
            case_reports=case_reports,
            aggregate_metrics=aggregate_metrics,
            overall_score=overall_score,
        )


# ============================================================
# 主函数
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="评估报告生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 从单个结果文件生成报告
  python report_generator.py --results ./experiment/comparison_results/TC_XXX.json
  
  # 从批量结果目录生成报告
  python report_generator.py --batch --results-dir ./experiment/comparison_results \\
                            --output ./experiment/reports/batch_report.html
  
  # 指定输出格式
  python report_generator.py --results ./TC_XXX.json --format markdown --output ./report.md
        """
    )
    
    parser.add_argument("--results", type=str, help="单个结果文件路径")
    parser.add_argument("--results-dir", type=str, help="批量结果目录路径")
    parser.add_argument("--batch", action="store_true", help="批量模式")
    parser.add_argument("--suite-name", type=str, default="Test Suite", help="测试套件名称")
    parser.add_argument("--output", type=str, help="输出文件路径")
    parser.add_argument("--format", type=str, choices=["text", "markdown", "html", "json"], 
                       default="markdown", help="输出格式")
    parser.add_argument("--template", type=str, choices=["simple", "detailed", "full"],
                       default="detailed", help="报告模板")
    
    args = parser.parse_args()
    
    # 确定输出路径
    output_path = Path(args.output) if args.output else None
    
    # 加载结果数据
    if args.results:
        with open(args.results, "r", encoding="utf-8") as f:
            result_data = json.load(f)
        
        # 转换并生成报告
        report_data = ReportDataConverter.from_comparison_result(result_data)
    elif args.results_dir:
        results_dir = Path(args.results_dir)
        result_files = list(results_dir.glob("*.json"))
        
        results = []
        for result_file in result_files:
            try:
                with open(result_file, "r", encoding="utf-8") as f:
                    results.append(json.load(f))
            except Exception as e:
                print(f"警告: 无法加载 {result_file}: {e}")
        
        # 转换并生成报告
        report_data = ReportDataConverter.from_batch_results(results, args.suite_name)
    else:
        print("错误: 请指定 --results 或 --results-dir")
        sys.exit(1)
    
    # 确定输出格式
    fmt = ReportFormat(args.format)
    
    # 如果没有指定输出路径，根据格式自动生成
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if args.batch:
            output_path = Path(f"report_batch_{timestamp}.{fmt.value}")
        else:
            output_path = Path(f"report_{timestamp}.{fmt.value}")
    
    # 生成报告
    generator = ReportGeneratorFactory.create(fmt)
    generator.generate(report_data, output_path, fmt)
    
    print(f"报告已生成: {output_path}")


if __name__ == "__main__":
    main()
