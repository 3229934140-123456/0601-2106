import click
import sys
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn
from rich import box
from typing import Optional

from . import __version__
from .config_manager import ConfigManager, SAMPLES_DIR
from .importer import DataImporter
from .rules import RiskRuleEngine
from .batch_manager import BatchManager
from .models import RiskLevel, BatchResult


console = Console()
cfg = ConfigManager()
batch_mgr = BatchManager()


@click.group()
@click.version_option(__version__, prog_name="riskctl")
def cli():
    """金融风控命令行工具 - 商户申请名单批量筛查"""
    pass


@cli.command()
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--batch-id", "-b", default=None, help="自定义批次号，默认自动生成")
@click.option("--no-save", is_flag=True, help="不保存批次结果到本地")
@click.option("--no-export", is_flag=True, help="不导出CSV分类报告")
@click.option("--output-dir", "-o", default=None, type=click.Path(), help="报告输出目录")
def check(input_file, batch_id, no_save, no_export, output_dir):
    """导入商户清单，批量筛查并生成风险等级"""
    console.rule("[bold blue]商户风控批量筛查")
    console.print(f"[cyan]输入文件:[/cyan] {input_file}")

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console
        ) as progress:
            t1 = progress.add_task("读取CSV文件...", total=None)
            merchants, import_errors = DataImporter.read_csv(input_file)
            progress.update(t1, completed=True, description="CSV读取完成")

            if not merchants:
                console.print("[red]没有可处理的有效商户数据[/red]")
                if import_errors:
                    _show_import_errors(import_errors)
                sys.exit(1)

            total = len(merchants)
            console.print(f"[green]✓[/green] 成功读取 [bold]{total}[/bold] 条商户记录")
            if import_errors:
                console.print(f"[yellow]⚠[/yellow] CSV解析错误 [bold]{len(import_errors)}[/bold] 行")

            t2 = progress.add_task("风控规则评估...", total=total)
            engine = RiskRuleEngine(cfg)

            results = []
            errors = []
            step = max(1, total // 50)

            contact_counter = engine._build_contact_counters(merchants)

            for i, m in enumerate(merchants):
                try:
                    r = engine._evaluate_single(m, contact_counter)
                    results.append(r)
                except Exception as e:
                    errors.append({
                        "row_number": m.row_number,
                        "merchant_id": m.merchant_id,
                        "merchant_name": m.merchant_name,
                        "error": str(e)
                    })
                if (i + 1) % step == 0 or (i + 1) == total:
                    progress.update(t2, completed=i + 1)

            progress.update(t2, completed=True, description="风控评估完成")

        all_errors = import_errors + errors
        batch = BatchResult(
            batch_id=batch_id or batch_mgr.generate_batch_id(),
            input_file=input_file,
            total_count=total,
            error_rows=all_errors,
            error_count=len(all_errors),
            results=results
        )

        pass_list = [r for r in results if r.final_decision == RiskLevel.PASS]
        review_list = [r for r in results if r.final_decision == RiskLevel.REVIEW]
        reject_list = [r for r in results if r.final_decision == RiskLevel.REJECT]

        batch.pass_count = len(pass_list)
        batch.review_count = len(review_list)
        batch.reject_count = len(reject_list)

        _show_summary(batch)

        if all_errors:
            _show_import_errors(all_errors)

        if not no_save:
            saved = batch_mgr.save_batch(batch)
            console.print(f"\n[green]✓[/green] 批次结果已保存: [link=file://{saved}]{saved}[/link]")

        if not no_export:
            exported = batch_mgr.export_csv(batch, output_dir)
            console.print(f"[green]✓[/green] CSV报告已导出:")
            for k, v in exported.items():
                label = {"pass": "通过", "review": "复核", "reject": "拒绝", "errors": "错误"}.get(k, k)
                console.print(f"    - {label}: [link=file://{v}]{v}[/link]")

        console.print(f"\n[dim]批次号: {batch.batch_id}[/dim]")
        console.print("[dim]使用 'riskctl explain <批次号> <商户ID>' 查看单个商户详情[/dim]")
        console.print("[dim]使用 'riskctl report <批次号>' 输出分类报告[/dim]")

    except FileNotFoundError as e:
        console.print(f"[red]✗ 文件错误:[/red] {e}")
        sys.exit(1)
    except ValueError as e:
        console.print(f"[red]✗ 数据错误:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]✗ 未知错误:[/red] {e}")
        import traceback
        console.print(traceback.format_exc())
        sys.exit(1)


def _show_summary(batch: BatchResult):
    s = batch.summary()
    summary_table = Table(
        title="[bold]筛查结果摘要[/bold]",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan"
    )
    summary_table.add_column("指标", style="bold")
    summary_table.add_column("数值", justify="right")
    summary_table.add_column("占比", justify="right")

    summary_table.add_row("总记录数", str(s["total_count"]), "-")
    summary_table.add_row("[green]通过[/green]", f"[green]{s['pass_count']}[/green]", f"[green]{s['pass_rate']}[/green]")
    summary_table.add_row("[yellow]需复核[/yellow]", f"[yellow]{s['review_count']}[/yellow]", f"[yellow]{s['review_rate']}[/yellow]")
    summary_table.add_row("[red]拒绝[/red]", f"[red]{s['reject_count']}[/red]", f"[red]{s['reject_rate']}[/red]")
    summary_table.add_row("[red]错误[/red]", f"[red]{s['error_count']}[/red]", "-")

    console.print()
    console.print(summary_table)

    if batch.results:
        top_high = sorted(batch.results, key=lambda x: -x.risk_score)[:5]
        detail_table = Table(
            title="[bold]高风险TOP5[/bold]",
            box=box.ROUNDED,
            header_style="bold magenta"
        )
        detail_table.add_column("行号", justify="right")
        detail_table.add_column("商户编号")
        detail_table.add_column("商户名称")
        detail_table.add_column("分值", justify="right", style="bold")
        detail_table.add_column("结论")
        detail_table.add_column("原因")

        for r in top_high:
            color = r.final_decision.color
            detail_table.add_row(
                str(r.merchant.row_number),
                r.merchant.merchant_id,
                r.merchant.merchant_name,
                str(r.risk_score),
                f"[{color}]{r.final_decision.display_name}[/{color}]",
                r.review_reason or "-"
            )
        console.print(detail_table)


def _show_import_errors(errors):
    err_table = Table(
        title=f"[bold red]错误行提示 ({len(errors)} 条)[/bold red]",
        box=box.ROUNDED,
        header_style="bold red"
    )
    err_table.add_column("行号", justify="right")
    err_table.add_column("商户编号")
    err_table.add_column("商户名称")
    err_table.add_column("错误信息")

    for e in errors[:20]:
        err_table.add_row(
            str(e["row_number"]),
            e.get("merchant_id", "-"),
            e.get("merchant_name", "-"),
            e["error"]
        )
    if len(errors) > 20:
        err_table.add_row("...", f"... 还有 {len(errors) - 20} 条错误", "", "...")
    console.print(err_table)


@cli.command()
@click.argument("batch_id")
@click.option("--category", "-c", type=click.Choice(["all", "pass", "review", "reject", "errors"]),
              default="all", help="输出类别：all=全部(默认), pass=通过, review=需复核, reject=拒绝, errors=错误")
@click.option("--format", "-f", "fmt", type=click.Choice(["table", "csv", "json"]),
              default="table", help="输出格式：table(默认), csv, json")
@click.option("--output-dir", "-o", default=None, type=click.Path(), help="导出目录（仅csv格式）")
@click.option("--limit", "-n", type=int, default=50, help="表格显示行数限制（默认50）")
def report(batch_id, category, fmt, output_dir, limit):
    """按批次输出通过、需复核、拒绝三类名单"""
    bid = batch_mgr.find_batch_partial(batch_id) or batch_id
    batch = batch_mgr.load_batch(bid)

    if not batch:
        console.print(f"[red]✗ 找不到批次:[/red] {batch_id}")
        console.print("[dim]使用 'riskctl history' 查看历史批次列表[/dim]")
        sys.exit(1)

    console.rule(f"[bold blue]批次报告: {batch.batch_id}")
    console.print(f"[cyan]输入文件:[/cyan] {batch.input_file}")
    console.print(f"[cyan]创建时间:[/cyan] {batch.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
    _show_summary(batch)

    if fmt == "csv":
        cats = None if category == "all" else [category]
        exported = batch_mgr.export_csv(batch, output_dir, cats)
        console.print(f"\n[green]✓ CSV导出完成:[/green]")
        for k, v in exported.items():
            label = {"pass": "通过", "review": "复核", "reject": "拒绝", "errors": "错误"}.get(k, k)
            console.print(f"  {label}: [link=file://{v}]{v}[/link]")
        return

    if fmt == "json":
        import json as json_lib
        data = _build_json_report(batch, category)
        console.print(json_lib.dumps(data, ensure_ascii=False, indent=2))
        return

    _show_tables(batch, category, limit)


def _build_json_report(batch: BatchResult, category: str) -> dict:
    data = {
        "summary": batch.summary(),
        "data": {}
    }
    if category in ["all", "pass"]:
        data["data"]["pass"] = [r.to_dict() for r in batch.get_pass_list()]
    if category in ["all", "review"]:
        data["data"]["review"] = [r.to_dict() for r in batch.get_review_list()]
    if category in ["all", "reject"]:
        data["data"]["reject"] = [r.to_dict() for r in batch.get_reject_list()]
    if category in ["all", "errors"]:
        data["data"]["errors"] = batch.error_rows
    return data


def _show_tables(batch: BatchResult, category: str, limit: int):
    def _build_result_table(title: str, results, color: str):
        t = Table(title=f"[bold][{color}]{title} ({len(results)}条)[/{color}][/bold]",
                  box=box.ROUNDED, header_style=f"bold {color}")
        t.add_column("行号", justify="right")
        t.add_column("商户编号")
        t.add_column("商户名称")
        t.add_column("分值", justify="right")
        t.add_column("结论")
        t.add_column("原因")
        for r in results[:limit]:
            c = r.final_decision.color
            t.add_row(
                str(r.merchant.row_number),
                r.merchant.merchant_id,
                r.merchant.merchant_name,
                str(r.risk_score),
                f"[{c}]{r.final_decision.display_name}[/{c}]",
                (r.review_reason or "-")[:40]
            )
        if len(results) > limit:
            t.add_row("...", f"... 还有 {len(results) - limit} 条", "", "", "", "...")
        return t

    if category in ["all", "pass"]:
        console.print()
        console.print(_build_result_table("通过名单", batch.get_pass_list(), "green"))
    if category in ["all", "review"]:
        console.print()
        console.print(_build_result_table("需复核名单", batch.get_review_list(), "yellow"))
    if category in ["all", "reject"]:
        console.print()
        console.print(_build_result_table("拒绝名单", batch.get_reject_list(), "red"))
    if category in ["all", "errors"] and batch.error_rows:
        console.print()
        _show_import_errors(batch.error_rows)


@cli.command()
@click.argument("batch_id")
@click.argument("merchant_identifier")
def explain(batch_id, merchant_identifier):
    """展示单个商户命中项和建议动作"""
    bid = batch_mgr.find_batch_partial(batch_id) or batch_id
    batch = batch_mgr.load_batch(bid)

    if not batch:
        console.print(f"[red]✗ 找不到批次:[/red] {batch_id}")
        console.print("[dim]使用 'riskctl history' 查看历史批次列表[/dim]")
        sys.exit(1)

    result = None
    for r in batch.results:
        if (r.merchant.merchant_id == merchant_identifier or
                r.merchant.merchant_name == merchant_identifier or
                str(r.merchant.row_number) == merchant_identifier):
            result = r
            break

    if not result:
        console.print(f"[red]✗ 批次中找不到商户:[/red] {merchant_identifier}")
        matches = [f"  {r.merchant.row_number}: {r.merchant.merchant_id} - {r.merchant.merchant_name}"
                   for r in batch.results[:10]]
        console.print("[yellow]可用商户:[/yellow]\n" + "\n".join(matches))
        if len(batch.results) > 10:
            console.print(f"  ... 共 {len(batch.results)} 条")
        sys.exit(1)

    m = result.merchant
    console.rule(f"[bold blue]商户风险详情: {m.merchant_name}")

    info_table = Table(box=box.ROUNDED, show_header=False, title_style="bold")
    info_table.add_column("字段", style="bold cyan", width=14)
    info_table.add_column("值")
    info_table.add_row("批次号", batch.batch_id)
    info_table.add_row("行号", str(m.row_number))
    info_table.add_row("商户编号", m.merchant_id)
    info_table.add_row("商户名称", m.merchant_name)
    info_table.add_row("风险分值", f"[bold]{result.risk_score}[/bold]")
    color = result.final_decision.color
    info_table.add_row("最终结论", f"[{color}][bold]{result.final_decision.display_name}[/bold][/{color}]")
    info_table.add_row("白名单豁免", "是" if result.is_whitelisted else "否")
    info_table.add_row("复核/拒绝原因", result.review_reason or "-")
    info_table.add_row("命中规则数", str(len(result.rule_hits)))
    info_table.add_row("处理时间", result.processed_at.strftime("%Y-%m-%d %H:%M:%S"))
    console.print(Panel(info_table, title="[bold]基本信息[/bold]", border_style="cyan"))

    if result.rule_hits:
        rule_table = Table(
            title="[bold]命中规则明细[/bold]",
            box=box.ROUNDED,
            header_style="bold magenta"
        )
        rule_table.add_column("#", justify="right", width=3)
        rule_table.add_column("规则代码", style="bold")
        rule_table.add_column("规则名称", style="bold")
        rule_table.add_column("严重度", justify="right")
        rule_table.add_column("命中描述")

        for i, h in enumerate(result.rule_hits, 1):
            sev_color = "red" if h.severity >= 40 else ("yellow" if h.severity >= 20 else "white")
            rule_table.add_row(
                str(i),
                h.rule_code,
                h.rule_name,
                f"[{sev_color}]{h.severity}[/{sev_color}]",
                h.message
            )
        console.print(rule_table)

        action_table = Table(
            title="[bold]建议动作[/bold]",
            box=box.ROUNDED,
            header_style="bold green",
            show_lines=True
        )
        action_table.add_column("#", justify="right", width=3)
        action_table.add_column("规则")
        action_table.add_column("建议动作")

        seen_actions = set()
        idx = 0
        for h in result.rule_hits:
            if h.suggested_action and h.suggested_action not in seen_actions:
                idx += 1
                seen_actions.add(h.suggested_action)
                action_table.add_row(str(idx), f"[{h.rule_code}] {h.rule_name}", h.suggested_action)

        if idx > 0:
            console.print(action_table)
    else:
        console.print(Panel("[green]未命中任何风险规则，状态良好[/green]", title="规则命中情况", border_style="green"))


@cli.group()
def config():
    """维护阈值、复核原因、黑名单和白名单"""
    pass


@config.command("show")
@click.option("--section", "-s", type=click.Choice(["all", "thresholds", "reasons", "blacklist", "whitelist", "actions"]),
              default="all", help="显示指定配置段")
def config_show(section):
    """显示当前配置"""
    cfg_data = cfg.get_all()

    if section in ["all", "thresholds"]:
        t = Table(title="[bold]阈值配置 (thresholds)[/bold]", box=box.ROUNDED, header_style="bold blue")
        t.add_column("参数", style="bold")
        t.add_column("值", justify="right")
        for k, v in cfg_data["thresholds"].items():
            t.add_row(k, str(v))
        console.print(t)

    if section in ["all", "reasons"]:
        t1 = Table(title="[bold]复核原因 (review_reasons)[/bold]", box=box.ROUNDED, header_style="bold yellow")
        t1.add_column("#", justify="right", width=4)
        t1.add_column("原因")
        for i, r in enumerate(cfg_data["review_reasons"], 1):
            t1.add_row(str(i), r)
        console.print(t1)

        t2 = Table(title="[bold]拒绝原因 (reject_reasons)[/bold]", box=box.ROUNDED, header_style="bold red")
        t2.add_column("#", justify="right", width=4)
        t2.add_column("原因")
        for i, r in enumerate(cfg_data["reject_reasons"], 1):
            t2.add_row(str(i), r)
        console.print(t2)

    if section in ["all", "actions"]:
        t = Table(title="[bold]建议动作 (suggested_actions)[/bold]", box=box.ROUNDED, header_style="bold green")
        t.add_column("规则代码", style="bold")
        t.add_column("建议动作", overflow="fold")
        for k, v in cfg_data["suggested_actions"].items():
            t.add_row(k, v)
        console.print(t)

    if section in ["all", "blacklist"]:
        bl = cfg_data["blacklist"]
        t = Table(title=f"[bold]黑名单 ({len(bl)}条)[/bold]", box=box.ROUNDED, header_style="bold red")
        t.add_column("#", justify="right", width=4)
        t.add_column("条目")
        if bl:
            for i, item in enumerate(bl, 1):
                t.add_row(str(i), item)
        else:
            t.add_row("-", "(空)")
        console.print(t)

    if section in ["all", "whitelist"]:
        wl = cfg_data["whitelist"]
        t = Table(title=f"[bold]白名单 ({len(wl)}条)[/bold]", box=box.ROUNDED, header_style="bold green")
        t.add_column("#", justify="right", width=4)
        t.add_column("条目")
        if wl:
            for i, item in enumerate(wl, 1):
                t.add_row(str(i), item)
        else:
            t.add_row("-", "(空)")
        console.print(t)


@config.command("set-threshold")
@click.argument("key")
@click.argument("value")
def config_set_threshold(key, value):
    """设置阈值参数，如 min_operation_years 1"""
    try:
        if "." in value:
            val = float(value)
        else:
            val = int(value)
    except ValueError:
        val = value
    cfg.set_threshold(key, val)
    console.print(f"[green]✓ 阈值已设置:[/green] thresholds.{key} = {val}")


@config.command("add-blacklist")
@click.argument("item")
def config_add_blacklist(item):
    """添加黑名单条目（商户编号/名称/身份证/手机号等）"""
    if cfg.add_blacklist(item):
        console.print(f"[green]✓ 已添加到黑名单:[/green] {item}")
    else:
        console.print(f"[yellow]⚠ 条目已存在:[/yellow] {item}")


@config.command("remove-blacklist")
@click.argument("item")
def config_remove_blacklist(item):
    """移除黑名单条目"""
    if cfg.remove_blacklist(item):
        console.print(f"[green]✓ 已从黑名单移除:[/green] {item}")
    else:
        console.print(f"[yellow]⚠ 条目不存在:[/yellow] {item}")


@config.command("add-whitelist")
@click.argument("item")
def config_add_whitelist(item):
    """添加白名单条目（商户编号或名称）"""
    if cfg.add_whitelist(item):
        console.print(f"[green]✓ 已添加到白名单:[/green] {item}")
    else:
        console.print(f"[yellow]⚠ 条目已存在:[/yellow] {item}")


@config.command("remove-whitelist")
@click.argument("item")
def config_remove_whitelist(item):
    """移除白名单条目"""
    if cfg.remove_whitelist(item):
        console.print(f"[green]✓ 已从白名单移除:[/green] {item}")
    else:
        console.print(f"[yellow]⚠ 条目不存在:[/yellow] {item}")


@config.command("add-review-reason")
@click.argument("reason")
def config_add_review_reason(reason):
    """添加复核原因"""
    if cfg.add_review_reason(reason):
        console.print(f"[green]✓ 已添加复核原因:[/green] {reason}")
    else:
        console.print(f"[yellow]⚠ 原因已存在:[/yellow] {reason}")


@config.command("remove-review-reason")
@click.argument("reason")
def config_remove_review_reason(reason):
    """移除复核原因"""
    if cfg.remove_review_reason(reason):
        console.print(f"[green]✓ 已移除复核原因:[/green] {reason}")
    else:
        console.print(f"[yellow]⚠ 原因不存在:[/yellow] {reason}")


@config.command("reset")
@click.confirmation_option(prompt="确定要重置所有配置为默认值吗？")
def config_reset():
    """重置所有配置为默认值"""
    cfg.reset()
    console.print("[green]✓ 配置已重置为默认值[/green]")


@cli.command()
@click.option("--limit", "-n", type=int, default=20, help="显示最近N个批次（默认20）")
def history(limit):
    """查询本地历史批次"""
    batches = batch_mgr.list_batches(limit)

    if not batches:
        console.print("[yellow]暂无历史批次记录[/yellow]")
        console.print("[dim]使用 'riskctl check <CSV文件>' 开始首次筛查[/dim]")
        return

    t = Table(
        title=f"[bold]历史批次（最近{len(batches)}个）[/bold]",
        box=box.ROUNDED,
        header_style="bold blue"
    )
    t.add_column("#", justify="right", width=4)
    t.add_column("批次号", style="bold")
    t.add_column("输入文件", overflow="fold")
    t.add_column("总数", justify="right")
    t.add_column("通过", justify="right", style="green")
    t.add_column("复核", justify="right", style="yellow")
    t.add_column("拒绝", justify="right", style="red")
    t.add_column("错误", justify="right")
    t.add_column("创建时间")

    for i, b in enumerate(batches, 1):
        t.add_row(
            str(i),
            b["batch_id"],
            Path(b["input_file"]).name,
            str(b["total"]),
            str(b["pass"]),
            str(b["review"]),
            str(b["reject"]),
            str(b["error"]),
            b["created_at"].replace("T", " ")[:19]
        )
    console.print(t)
    console.print("\n[dim]提示: 可使用批次号前缀匹配，如 'riskctl report 202501'[/dim]")


@cli.command()
def sample():
    """生成示例CSV文件"""
    import csv
    sample_path = SAMPLES_DIR / "merchants_sample.csv"
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    with open(sample_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "商户编号", "商户名称", "经营年限", "上月营收", "前月营收", "3月前营收",
            "法人变更次数", "法人姓名", "经营地址", "地址状态", "联系电话", "联系人", "身份证号", "所属行业"
        ])
        writer.writerow(["M001", "阳光便利店", "3.5", "125000", "118000", "122000", "0", "张三", "北京市朝阳区建国路88号", "正常", "13800138001", "张三", "110101199001011234", "零售"])
        writer.writerow(["M002", "诚信批发部", "0.5", "80000", "75000", "78000", "1", "李四", "上海市浦东新区陆家嘴100号", "正常", "13800138002", "李四", "310101199202022345", "批发"])
        writer.writerow(["M003", "宏达商贸", "5.0", "320000", "150000", "300000", "1", "王五", "广州市天河区珠江新城200号", "正常", "13800138003", "王五", "440101198803033456", "商贸"])
        writer.writerow(["M004", "恒通建材", "2.0", "450000", "420000", "430000", "3", "赵六", "深圳市南山区科技园300号", "正常", "13800138004", "赵六", "440301198504044567", "建材"])
        writer.writerow(["M005", "鸿运餐饮", "1.2", "280000", "260000", "270000", "0", "孙七", "杭州市西湖区文三路400号", "待核实", "13800138005", "孙七", "330101199105055678", "餐饮"])
        writer.writerow(["M006", "利群超市", "4.0", "560000", "540000", "550000", "0", "周八", "成都市锦江区春熙路500号", "正常", "13800138001", "周八", "510101198706066789", "零售"])
        writer.writerow(["M007", "泰康药店", "0.3", "50000", "48000", "46000", "4", "吴九", "武汉市江汉区步行街600号", "异常", "13800138007", "吴九", "420101199307077890", "医药"])
        writer.writerow(["M008", "盛世物流", "6.0", "890000", "870000", "880000", "0", "郑十", "南京市鼓楼区中山路700号", "正常", "13800138008", "郑十", "320101198408088901", "物流"])
        writer.writerow(["M009", "创新科技", "2.5", "200000", "190000", "180000", "1", "张三", "西安市雁塔区高新路800号", "正常", "13800138009", "钱十一", "110101199001011234", "科技"])
        writer.writerow(["M010", "兴旺五金", "1.8", "310000", "290000", "300000", "0", "冯十二", "天津市和平区南京路900号", "正常", "13800138010", "冯十二", "120101198909099012", "五金"])
        writer.writerow(["M011", "", "1.0", "100000", "95000", "90000", "0", "陈十三", "重庆市渝中区解放碑1000号", "正常", "13800138011", "陈十三", "500101198810100123", "贸易"])
        writer.writerow(["M012", "华达服装", "abc", "150000", "140000", "130000", "0", "褚十四", "苏州市工业园区金鸡湖1100号", "正常", "13800138012", "褚十四", "320501199111111234", "服装"])

    console.print(f"[green]✓ 示例文件已生成:[/green] [link=file://{sample_path}]{sample_path}[/link]")
    console.print("\n[dim]包含12条商户数据，覆盖：正常、经营年限不足、交易波动、法人变更频繁、[/dim]")
    console.print("[dim]地址异常、联系方式重复、黑名单、格式错误等典型场景[/dim]")
    console.print("\n[bold]使用示例:[/bold]")
    console.print("  riskctl check data/samples/merchants_sample.csv")


def main():
    cli()


if __name__ == "__main__":
    main()
