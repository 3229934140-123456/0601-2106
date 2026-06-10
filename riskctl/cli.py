import click
import sys
import copy
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
@click.option("--config-version", "-V", default=None, help="使用指定配置版本运行")
def check(input_file, batch_id, no_save, no_export, output_dir, config_version):
    """导入商户清单，批量筛查并生成风险等级"""
    console.rule("[bold blue]商户风控批量筛查")
    console.print(f"[cyan]输入文件:[/cyan] {input_file}")

    used_config_version = ""
    original_config = None
    if config_version:
        vdata = cfg.get_version(config_version)
        if not vdata:
            console.print(f"[red]✗ 配置版本不存在:[/red] {config_version}")
            sys.exit(1)
        original_config = copy.deepcopy(cfg.get_all())
        cfg.load_version(config_version)
        used_config_version = vdata.get("name", config_version)
        console.print(f"[cyan]配置版本:[/cyan] {used_config_version}")

    try:
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
                merchants, import_errors, total_raw_rows = DataImporter.read_csv(input_file)
                progress.update(t1, completed=True, description="CSV读取完成")

                if not merchants and not import_errors:
                    console.print("[red]CSV文件没有任何数据行[/red]")
                    sys.exit(1)

                if not merchants:
                    console.print("[red]没有可处理的有效商户数据[/red]")
                    if import_errors:
                        _show_import_errors(import_errors)
                    sys.exit(1)

                total_valid = len(merchants)
                console.print(f"[green]✓[/green] 原始清单共 [bold]{total_raw_rows}[/bold] 行")
                console.print(f"[green]✓[/green] 成功解析 [bold]{total_valid}[/bold] 条有效商户记录")
                if import_errors:
                    console.print(f"[yellow]⚠[/yellow] CSV解析错误 [bold]{len(import_errors)}[/bold] 行")

                t2 = progress.add_task("风控规则评估...", total=total_valid)
                engine = RiskRuleEngine(cfg)

                results = []
                errors = []
                step = max(1, total_valid // 50)

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
                    if (i + 1) % step == 0 or (i + 1) == total_valid:
                        progress.update(t2, completed=i + 1)

                progress.update(t2, completed=True, description="风控评估完成")

            all_errors = import_errors + errors
            batch = BatchResult(
                batch_id=batch_id or batch_mgr.generate_batch_id(),
                input_file=input_file,
                total_count=total_raw_rows,
                valid_count=len(results),
                error_rows=all_errors,
                error_count=len(all_errors),
                results=results,
                config_version=used_config_version
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
    finally:
        if original_config is not None:
            cfg._config = original_config
            cfg._save_config(original_config)


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
    summary_table.add_column("占原始清单", justify="right")
    summary_table.add_column("占有效记录", justify="right")

    check_sum = s["pass_count"] + s["review_count"] + s["reject_count"] + s["error_count"]
    total_match = "✓" if check_sum == s["total_count"] else "✗"

    summary_table.add_row(f"原始清单行数 ({total_match})", str(s["total_count"]), "-", "-")
    summary_table.add_row("有效评估数", str(s["valid_count"]), "-", "-")
    summary_table.add_row("[green]通过[/green]", f"[green]{s['pass_count']}[/green]", f"[green]{s['pass_rate']}[/green]", f"[green]{s['valid_pass_rate']}[/green]")
    summary_table.add_row("[yellow]需复核[/yellow]", f"[yellow]{s['review_count']}[/yellow]", f"[yellow]{s['review_rate']}[/yellow]", f"[yellow]{s['valid_review_rate']}[/yellow]")
    summary_table.add_row("[red]拒绝[/red]", f"[red]{s['reject_count']}[/red]", f"[red]{s['reject_rate']}[/red]", f"[red]{s['valid_reject_rate']}[/red]")
    summary_table.add_row("[red]错误行[/red]", f"[red]{s['error_count']}[/red]", f"[red]{s['error_rate']}[/red]", "-")
    summary_table.add_row("合计校验", str(check_sum), "-", "-")

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
@click.option("--value", "-v", "value", required=True, help="阈值数值（支持负数，不会被识别为选项）")
@click.option("--force", "-f", is_flag=True, help="强制写入，跳过区间一致性校验")
def config_set_threshold(key, value, force):
    """设置阈值参数，如 set-threshold min_operation_years -v 1

    KEY为阈值参数名，使用 -v/--value 指定数值（支持负数）。
    例: riskctl config set-threshold min_operation_years -v 1.5
        riskctl config set-threshold pass_score_max -v 40
    """
    try:
        val = cfg.validate_threshold_value(key, value)
    except ValueError as e:
        console.print(f"[red]✗ 阈值输入错误:[/red]\n{e}")
        sys.exit(1)

    issues = cfg.validate_score_consistency(updated_key=key, temp_value=val)
    if issues and not force:
        issue_lines = "\n".join(f"  • {i}" for i in issues)
        console.print(
            f"[yellow]⚠ 分数区间一致性警告:[/yellow]\n{issue_lines}\n\n"
            f"[dim]如需强制设置，请加 --force 参数:[/dim]\n"
            f"  riskctl config set-threshold {key} -v {value} --force"
        )
        sys.exit(1)

    cfg.set_threshold(key, val)
    console.print(f"[green]✓ 阈值已设置:[/green] thresholds.{key} = {val}")
    if key in ConfigManager.SCORE_RANGE_KEYS:
        t = cfg.get("thresholds", {})
        console.print(
            f"[dim]当前区间: 通过[{t['pass_score_min']}~{t['pass_score_max']}] → "
            f"复核[{t['review_score_min']}~{t['review_score_max']}] → "
            f"拒绝[≥{t['reject_score_min']}][/dim]"
        )


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


@config.command("save-version")
@click.argument("name")
@click.option("--desc", "-d", "description", default="", help="版本描述")
def config_save_version(name, description):
    """将当前配置保存为一个命名版本"""
    try:
        ok = cfg.save_version(name, description)
        if ok:
            console.print(f"[green]✓ 配置版本已保存:[/green] {name}")
        else:
            console.print(f"[yellow]⚠ 版本已存在:[/yellow] {name}")
            console.print("[dim]如需覆盖请先删除旧版本: riskctl config delete-version <name>[/dim]")
    except ValueError as e:
        console.print(f"[red]✗ 保存失败:[/red] {e}")
        sys.exit(1)


@config.command("list-versions")
def config_list_versions():
    """列出所有配置版本"""
    versions = cfg.list_versions()
    if not versions:
        console.print("[yellow]暂无配置版本[/yellow]")
        console.print("[dim]使用 'riskctl config save-version <名称>' 保存当前配置[/dim]")
        return
    t = Table(title="[bold]配置版本列表[/bold]", box=box.ROUNDED, header_style="bold cyan")
    t.add_column("版本名", style="bold")
    t.add_column("描述")
    t.add_column("创建时间", style="dim")
    for v in versions:
        t.add_row(v["name"], v.get("description", ""), v.get("created_at", "")[:19].replace("T", " "))
    console.print(t)


@config.command("diff-version")
@click.argument("version_a")
@click.argument("version_b")
def config_diff_version(version_a, version_b):
    """对比两个配置版本的差异"""
    try:
        diffs = cfg.diff_versions(version_a, version_b)
    except ValueError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)

    if not diffs:
        console.print(f"[green]✓ 两个版本完全一致[/green]")
        return

    t = Table(
        title=f"[bold]配置版本差异: {version_a} → {version_b}[/bold]",
        box=box.ROUNDED,
        header_style="bold magenta"
    )
    t.add_column("配置项", style="bold")
    t.add_column(f"{version_a}", style="dim")
    t.add_column("变化", style="yellow")
    t.add_column(f"{version_b}", style="green")

    for d in diffs:
        path = d["path"]
        if "added" in d:
            added_str = "\n".join(f"+ {x}" for x in d["added"]) if d["added"] else ""
            removed_str = "\n".join(f"- {x}" for x in d["removed"]) if d["removed"] else ""
            va_val = removed_str
            vb_val = added_str
            change = d["change"]
        else:
            va_val = str(d["value_a"])
            vb_val = str(d["value_b"])
            change = ""
        t.add_row(path, va_val, change, vb_val)

    console.print(t)
    console.print(f"[dim]共 {len(diffs)} 处差异[/dim]")


@config.command("rollback-version")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
def config_rollback_version(name, yes):
    """回滚到指定配置版本（覆盖当前配置）"""
    version_data = cfg.get_version(name)
    if not version_data:
        console.print(f"[red]✗ 版本不存在:[/red] {name}")
        sys.exit(1)

    if not yes:
        console.print(f"[yellow]即将回滚配置到版本: [bold]{name}[/bold][/yellow]")
        console.print(f"[dim]  描述: {version_data.get('description', '(无)')}[/dim]")
        console.print(f"[dim]  创建: {version_data.get('created_at', '')[:19]}[/dim]")
        if not click.confirm("确定要覆盖当前配置吗？", default=False):
            console.print("[dim]已取消[/dim]")
            return

    ok = cfg.load_version(name)
    if ok:
        console.print(f"[green]✓ 配置已回滚到版本:[/green] {name}")
    else:
        console.print(f"[red]✗ 回滚失败:[/red] {name}")
        sys.exit(1)


@config.command("delete-version")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
def config_delete_version(name, yes):
    """删除指定配置版本"""
    version_data = cfg.get_version(name)
    if not version_data:
        console.print(f"[red]✗ 版本不存在:[/red] {name}")
        sys.exit(1)

    if not yes:
        if not click.confirm(f"确定要删除配置版本 '{name}' 吗？", default=False):
            console.print("[dim]已取消[/dim]")
            return

    ok = cfg.delete_version(name)
    if ok:
        console.print(f"[green]✓ 版本已删除:[/green] {name}")
    else:
        console.print(f"[red]✗ 删除失败:[/red] {name}")
        sys.exit(1)


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
    t.add_column("原始行", justify="right")
    t.add_column("通过", justify="right", style="green")
    t.add_column("复核", justify="right", style="yellow")
    t.add_column("拒绝", justify="right", style="red")
    t.add_column("错误", justify="right", style="magenta")
    t.add_column("通过率", justify="right")
    t.add_column("配置版本", style="cyan")
    t.add_column("创建时间")

    for i, b in enumerate(batches, 1):
        total = b["total"]
        pass_rate = f"{(b['pass']/total*100):.0f}%" if total > 0 else "0%"
        cfg_ver = b.get("config_version", "") or "-"
        t.add_row(
            str(i),
            b["batch_id"],
            Path(b["input_file"]).name,
            str(total),
            str(b["pass"]),
            str(b["review"]),
            str(b["reject"]),
            str(b["error"]),
            pass_rate,
            cfg_ver,
            b["created_at"].replace("T", " ")[:19]
        )
    console.print(t)
    console.print("\n[dim]校验: 原始行 = 通过 + 复核 + 拒绝 + 错误[/dim]")
    console.print("[dim]提示: 可使用批次号前缀匹配，如 'riskctl report 202501'[/dim]")


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


@cli.command()
@click.argument("batch_id_a")
@click.argument("batch_id_b")
@click.option("--top-rules", "-n", type=int, default=10, help="显示命中次数前N的规则（默认10）")
def compare(batch_id_a, batch_id_b, top_rules):
    """对比两个历史批次的通过率、拒绝率和命中规则变化"""
    bid_a = batch_mgr.find_batch_partial(batch_id_a) or batch_id_a
    bid_b = batch_mgr.find_batch_partial(batch_id_b) or batch_id_b
    batch_a = batch_mgr.load_batch(bid_a)
    batch_b = batch_mgr.load_batch(bid_b)

    if not batch_a:
        console.print(f"[red]✗ 找不到批次A:[/red] {batch_id_a}")
        sys.exit(1)
    if not batch_b:
        console.print(f"[red]✗ 找不到批次B:[/red] {batch_id_b}")
        sys.exit(1)

    def _metrics(b):
        s = b.summary()
        total = s["total_count"]
        valid = s["valid_count"]
        def _pct(n, d):
            return (n / d * 100) if d > 0 else 0.0
        return {
            "total": total,
            "valid": valid,
            "pass": s["pass_count"],
            "review": s["review_count"],
            "reject": s["reject_count"],
            "error": s["error_count"],
            "pass_pct_t": _pct(s["pass_count"], total),
            "review_pct_t": _pct(s["review_count"], total),
            "reject_pct_t": _pct(s["reject_count"], total),
            "error_pct_t": _pct(s["error_count"], total),
            "pass_pct_v": _pct(s["pass_count"], valid),
            "review_pct_v": _pct(s["review_count"], valid),
            "reject_pct_v": _pct(s["reject_count"], valid),
            "created": s["created_at"]
        }

    ma = _metrics(batch_a)
    mb = _metrics(batch_b)

    short_a = bid_a[:16]
    short_b = bid_b[:16]

    console.rule(f"[bold blue]批次对比: {short_a}...  vs  {short_b}...")
    console.print(f"  [cyan]批次A[/cyan]: {bid_a} ({ma['created'][:19].replace('T',' ')})  原始行={ma['total']}  有效={ma['valid']}")
    console.print(f"  [cyan]批次B[/cyan]: {bid_b} ({mb['created'][:19].replace('T',' ')})  原始行={mb['total']}  有效={mb['valid']}")

    def _diff(old, new, is_pct=True):
        delta = new - old
        sign = "+" if delta >= 0 else ""
        unit = "pp" if is_pct else ""
        return delta, f"{sign}{delta:.1f}{unit}"

    def _style_delta(delta, reverse=False):
        if reverse:
            delta = -delta
        if delta >= 5:
            return "red"
        elif delta >= 1:
            return "yellow"
        elif delta <= -5:
            return "green"
        elif delta <= -1:
            return "cyan"
        return "white"

    console.print()
    mt = Table(title="[bold]核心指标对比（占原始清单）[/bold]", box=box.ROUNDED, header_style="bold cyan")
    mt.add_column("指标", style="bold")
    mt.add_column(f"批次A\n{short_a}...", justify="right")
    mt.add_column(f"批次B\n{short_b}...", justify="right")
    mt.add_column("变化", justify="right")
    mt.add_column("趋势", justify="center")

    for label, ka, kb, reverse in [
        ("通过率(原始)", "pass_pct_t", "pass_pct_t", False),
        ("复核率(原始)", "review_pct_t", "review_pct_t", False),
        ("拒绝率(原始)", "reject_pct_t", "reject_pct_t", True),
        ("错误率(原始)", "error_pct_t", "error_pct_t", True),
    ]:
        d, d_str = _diff(ma[ka], mb[kb])
        color = _style_delta(d, reverse=reverse)
        arrow = "↑" if d > 0 else ("↓" if d < 0 else "→")
        mt.add_row(label, f"{ma[ka]:.1f}%", f"{mb[kb]:.1f}%", d_str, f"[{color}]{arrow}[/{color}]")
    console.print(mt)

    mt2 = Table(title="[bold]核心指标对比（占有效记录）[/bold]", box=box.ROUNDED, header_style="bold magenta")
    mt2.add_column("指标", style="bold")
    mt2.add_column(f"批次A\n{short_a}...", justify="right")
    mt2.add_column(f"批次B\n{short_b}...", justify="right")
    mt2.add_column("变化", justify="right")
    mt2.add_column("趋势", justify="center")
    for label, ka, kb, reverse in [
        ("通过率(有效)", "pass_pct_v", "pass_pct_v", False),
        ("复核率(有效)", "review_pct_v", "review_pct_v", False),
        ("拒绝率(有效)", "reject_pct_v", "reject_pct_v", True),
    ]:
        d, d_str = _diff(ma[ka], mb[kb])
        color = _style_delta(d, reverse=reverse)
        arrow = "↑" if d > 0 else ("↓" if d < 0 else "→")
        mt2.add_row(label, f"{ma[ka]:.1f}%", f"{mb[kb]:.1f}%", d_str, f"[{color}]{arrow}[/{color}]")
    console.print(mt2)

    def _rule_stats(results):
        from collections import Counter
        counter = Counter()
        detail = {}
        for r in results:
            for h in r.rule_hits:
                key = f"{h.rule_code}|{h.rule_name}"
                counter[key] += 1
                if key not in detail:
                    detail[key] = {"code": h.rule_code, "name": h.rule_name, "severity_avg": []}
                detail[key]["severity_avg"].append(h.severity)
        stats = {}
        for k, cnt in counter.items():
            d = detail[k]
            stats[k] = {
                "code": d["code"],
                "name": d["name"],
                "count": cnt,
                "severity_avg": sum(d["severity_avg"]) / len(d["severity_avg"])
            }
        return stats

    sa = _rule_stats(batch_a.results)
    sb = _rule_stats(batch_b.results)
    all_keys = set(sa.keys()) | set(sb.keys())

    va = batch_a.valid_count or 1
    vb = batch_b.valid_count or 1

    rule_rows = []
    for k in all_keys:
        info = sa.get(k, sb[k])
        ca = sa.get(k, {}).get("count", 0)
        cb = sb.get(k, {}).get("count", 0)
        pa = ca / va * 100
        pb = cb / vb * 100
        delta = pb - pa
        cnt_delta = cb - ca
        rule_rows.append((info["code"], info["name"], ca, pa, cb, pb, cnt_delta, delta))

    rule_rows.sort(key=lambda x: -abs(x[7]))
    rule_rows = rule_rows[:top_rules]

    rt = Table(
        title=f"[bold]命中规则变化对比（TOP{len(rule_rows)}，按变化幅度）[/bold]",
        box=box.ROUNDED,
        header_style="bold yellow",
        show_lines=True
    )
    rt.add_column("规则代码", style="bold")
    rt.add_column("规则名称", overflow="fold")
    rt.add_column(f"A命中\n({va}条)", justify="right")
    rt.add_column(f"A占比", justify="right")
    rt.add_column(f"B命中\n({vb}条)", justify="right")
    rt.add_column(f"B占比", justify="right")
    rt.add_column("次数变化", justify="right")
    rt.add_column("占比变化", justify="right")
    rt.add_column("风险\n趋势", justify="center")

    for code, name, ca, pa, cb, pb, cd, pd in rule_rows:
        if pd >= 5:
            trend = "[red]▲▲ 升高[/red]"
        elif pd >= 1:
            trend = "[yellow]▲ 升高[/yellow]"
        elif pd <= -5:
            trend = "[green]▼▼ 下降[/green]"
        elif pd <= -1:
            trend = "[cyan]▼ 下降[/cyan]"
        else:
            trend = "[white]— 稳定[/white]"
        cd_str = f"{'+' if cd >= 0 else ''}{cd}"
        pd_str = f"{'+' if pd >= 0 else ''}{pd:.1f}pp"
        if pd >= 5:
            ca_s, cb_s = f"{ca}", f"[bold red]{cb}[/bold red]"
            pa_s, pb_s = f"{pa:.1f}%", f"[bold red]{pb:.1f}%[/bold red]"
            cd_str, pd_str = f"[red]{cd_str}[/red]", f"[bold red]{pd_str}[/bold red]"
        elif pd <= -5:
            ca_s, cb_s = f"{ca}", f"[green]{cb}[/green]"
            pa_s, pb_s = f"{pa:.1f}%", f"[green]{pb:.1f}%[/green]"
            cd_str, pd_str = f"[green]{cd_str}[/green]", f"[green]{pd_str}[/green]"
        else:
            ca_s, cb_s = str(ca), str(cb)
            pa_s, pb_s = f"{pa:.1f}%", f"{pb:.1f}%"
        rt.add_row(code, name, ca_s, pa_s, cb_s, pb_s, cd_str, pd_str, trend)
    console.print(rt)

    top_up = [r for r in rule_rows if r[7] >= 1][:3]
    top_down = [r for r in rule_rows if r[7] <= -1][:3]
    if top_up or top_down:
        console.print()
        insigths = []
        if top_up:
            insigths.append(f"[red]风险明显升高规则:[/red] " +
                "、".join(f"{r[1]}(+{r[7]:.1f}pp)" for r in top_up))
        if top_down:
            insigths.append(f"[green]风险明显下降规则:[/green] " +
                "、".join(f"{r[1]}({r[7]:.1f}pp)" for r in top_down))
        console.print(Panel("\n".join(insigths), title="[bold]观察结论[/bold]", border_style="cyan"))


@cli.command()
@click.option("--limit", "-n", type=int, default=20, help="显示最近N个批次（默认20）")
@click.option("--days", type=int, default=None, help="最近N天的批次")
@click.option("--from", "from_date", default=None, help="起始日期（YYYY-MM-DD）")
@click.option("--to", "to_date", default=None, help="结束日期（YYYY-MM-DD）")
@click.option("--top-rules", type=int, default=10, help="显示前N个规则（默认10）")
def trend(limit, days, from_date, to_date, top_rules):
    """多批次趋势汇总：通过率/拒绝率/规则命中变化"""
    from datetime import datetime, timedelta

    batches_list = batch_mgr.list_batches(limit=200)
    if not batches_list:
        console.print("[yellow]暂无历史批次记录[/yellow]")
        return

    filtered = []
    for b in batches_list:
        bdt = datetime.fromisoformat(b["created_at"].replace("Z", ""))
        include = True
        if days:
            cutoff = datetime.now() - timedelta(days=days)
            if bdt < cutoff:
                include = False
        if from_date:
            try:
                fd = datetime.strptime(from_date, "%Y-%m-%d")
                if bdt.date() < fd.date():
                    include = False
            except ValueError:
                console.print(f"[red]✗ 起始日期格式错误:[/red] {from_date}（应为 YYYY-MM-DD）")
                sys.exit(1)
        if to_date:
            try:
                td = datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)
                if bdt >= td:
                    include = False
            except ValueError:
                console.print(f"[red]✗ 结束日期格式错误:[/red] {to_date}（应为 YYYY-MM-DD）")
                sys.exit(1)
        if include:
            filtered.append(b)

    if not filtered:
        console.print("[yellow]筛选条件内没有批次记录[/yellow]")
        return

    if limit and len(filtered) > limit:
        filtered = filtered[:limit]

    filtered = list(reversed(filtered))

    console.rule(f"[bold blue]批次趋势汇总（{len(filtered)} 个批次）")
    if filtered:
        first_dt = filtered[0]["created_at"][:10]
        last_dt = filtered[-1]["created_at"][:10]
        console.print(f"  [cyan]时间范围:[/cyan] {first_dt} ~ {last_dt}")
        total_all = sum(b["total"] for b in filtered)
        console.print(f"  [cyan]总原始行:[/cyan] {total_all}")

    t = Table(
        title="[bold]批次指标趋势[/bold]",
        box=box.ROUNDED,
        header_style="bold cyan",
        show_lines=False
    )
    t.add_column("#", justify="right", width=3)
    t.add_column("日期", style="dim")
    t.add_column("批次号", style="bold")
    t.add_column("原始行", justify="right")
    t.add_column("通过", justify="right", style="green")
    t.add_column("复核", justify="right", style="yellow")
    t.add_column("拒绝", justify="right", style="red")
    t.add_column("错误", justify="right", style="magenta")
    t.add_column("通过率", justify="right")
    t.add_column("配置版本", style="cyan")

    for i, b in enumerate(filtered, 1):
        total = b["total"]
        pass_rate = f"{(b['pass']/total*100):.1f}%" if total > 0 else "0%"
        cfg_ver = b.get("config_version", "") or "-"
        t.add_row(
            str(i),
            b["created_at"][5:16].replace("T", " "),
            b["batch_id"][:18] + "..." if len(b["batch_id"]) > 18 else b["batch_id"],
            str(total),
            str(b["pass"]),
            str(b["review"]),
            str(b["reject"]),
            str(b["error"]),
            pass_rate,
            cfg_ver
        )
    console.print(t)

    if len(filtered) >= 2:
        first = filtered[0]
        last = filtered[-1]
        ft = first["total"] or 1
        lt = last["total"] or 1
        fp = first["pass"] / ft * 100
        lp = last["pass"] / lt * 100
        fr = first["reject"] / ft * 100
        lr = last["reject"] / lt * 100
        fe = first["error"] / ft * 100
        le = last["error"] / lt * 100

        delta_p = lp - fp
        delta_r = lr - fr
        delta_e = le - fe

        def _fmt_delta(d, reverse=False):
            sign = "+" if d >= 0 else ""
            color = "red" if (d > 0 and not reverse) or (d < 0 and reverse) else "green"
            if abs(d) < 0.1:
                return f"[dim]{sign}{d:.1f}pp[/dim]"
            return f"[{color}]{sign}{d:.1f}pp[/{color}]"

        sum_table = Table(
            title="[bold]首末批次对比[/bold]",
            box=box.SIMPLE,
            header_style="bold magenta"
        )
        sum_table.add_column("指标", style="bold")
        sum_table.add_column("首批", justify="right")
        sum_table.add_column("末批", justify="right")
        sum_table.add_column("变化", justify="right")
        sum_table.add_row("通过率", f"{fp:.1f}%", f"{lp:.1f}%", _fmt_delta(delta_p, reverse=True))
        sum_table.add_row("拒绝率", f"{fr:.1f}%", f"{lr:.1f}%", _fmt_delta(delta_r, reverse=False))
        sum_table.add_row("错误率", f"{fe:.1f}%", f"{le:.1f}%", _fmt_delta(delta_e, reverse=False))
        console.print(sum_table)

    console.rule("[bold][magenta]规则命中趋势[/magenta][/bold]")

    rule_stats = {}
    for b in filtered:
        batch = batch_mgr.load_batch(b["batch_id"])
        if not batch:
            continue
        total = b["total"] or 1
        for r in batch.results:
            for h in r.rule_hits:
                code = h.rule_code
                if code not in rule_stats:
                    rule_stats[code] = {
                        "name": h.rule_name,
                        "batch_counts": [0] * len(filtered),
                        "batch_ratios": [0.0] * len(filtered),
                        "total_count": 0
                    }
                batch_idx = [i for i, x in enumerate(filtered) if x["batch_id"] == b["batch_id"]][0]
                rule_stats[code]["batch_counts"][batch_idx] += 1
                rule_stats[code]["total_count"] += 1

    if not rule_stats:
        console.print("[dim]没有规则命中数据[/dim]")
        return

    for code, stats in rule_stats.items():
        for i, b in enumerate(filtered):
            total = b["total"] or 1
            stats["batch_ratios"][i] = stats["batch_counts"][i] / total * 100

    def _trend_direction(ratios):
        if len(ratios) < 2:
            return 0
        first_half = sum(ratios[:len(ratios)//2]) / (len(ratios)//2 or 1)
        second_half = sum(ratios[len(ratios)//2:]) / len(ratios[len(ratios)//2:])
        return second_half - first_half

    sorted_rules = sorted(
        rule_stats.items(),
        key=lambda x: -_trend_direction(x[1]["batch_ratios"])
    )[:top_rules]

    rt = Table(
        title=f"[bold]规则命中趋势（TOP{len(sorted_rules)}，按升幅排序）[/bold]",
        box=box.ROUNDED,
        header_style="bold red"
    )
    rt.add_column("规则", style="bold")
    for i, b in enumerate(filtered, 1):
        rt.add_column(f"P{i}", justify="right")
    rt.add_column("变化", justify="right")
    rt.add_column("趋势", justify="center")

    for code, stats in sorted_rules:
        row_vals = []
        for i in range(len(filtered)):
            pct = stats["batch_ratios"][i]
            if pct == 0:
                row_vals.append("[dim]-[/dim]")
            else:
                row_vals.append(f"{pct:.1f}%")
        delta = _trend_direction(stats["batch_ratios"])
        if delta >= 2:
            trend_mark = "[red]▲▲[/red]"
            delta_str = f"[bold red]+{delta:.1f}pp[/bold red]"
        elif delta >= 0.5:
            trend_mark = "[yellow]▲[/yellow]"
            delta_str = f"[yellow]+{delta:.1f}pp[/yellow]"
        elif delta <= -2:
            trend_mark = "[green]▼▼[/green]"
            delta_str = f"[green]{delta:.1f}pp[/green]"
        elif delta <= -0.5:
            trend_mark = "[green]▼[/green]"
            delta_str = f"[green]{delta:.1f}pp[/green]"
        else:
            trend_mark = "[dim]—[/dim]"
            delta_str = f"[dim]{delta:+.1f}pp[/dim]"

        rt.add_row(stats["name"], *row_vals, delta_str, trend_mark)

    console.print(rt)

    rising_rules = [(code, stats) for code, stats in sorted_rules
                    if _trend_direction(stats["batch_ratios"]) >= 1]
    if rising_rules:
        console.print()
        rising_text = "\n".join(
            f"  • [red]{s['name']}[/red]（+{_trend_direction(s['batch_ratios']):.1f}pp）"
            for _, s in rising_rules[:5]
        )
        console.print(Panel(
            rising_text,
            title="[bold red]⚠ 风险升高规则[/bold red]",
            border_style="red",
            subtitle=f"共 {len(rising_rules)} 项规则命中呈上升趋势"
        ))

    console.print("\n[dim]提示: 趋势=后段平均 - 前段平均；▲▲ ≥2pp 显著升高，▲ ≥0.5pp 微升[/dim]")


@cli.command()
@click.argument("merchant_id")
@click.option("--show-hits", "-d", is_flag=True, help="显示每次筛查的命中项详情")
@click.option("--export", "-o", "export_path", default=None, type=click.Path(), help="导出轨迹到文件（CSV/JSON）")
@click.option("--format", "-f", "export_format", default=None, type=click.Choice(["csv", "json"]), help="导出格式（默认按扩展名推断）")
def merchant(merchant_id, show_hits, export_path, export_format):
    """按商户编号查询历史筛查记录与分数变化"""
    batches = batch_mgr.list_batches(limit=200)
    records = []

    for b_meta in batches:
        batch = batch_mgr.load_batch(b_meta["batch_id"])
        if not batch:
            continue
        for r in batch.results:
            if r.merchant.merchant_id == merchant_id or r.merchant.merchant_name == merchant_id:
                records.append({
                    "batch_id": batch.batch_id,
                    "created_at": batch.created_at,
                    "input_file": batch.input_file,
                    "merchant_name": r.merchant.merchant_name,
                    "merchant_id": r.merchant.merchant_id,
                    "score": r.risk_score,
                    "level": r.final_decision,
                    "level_display": r.final_decision.display_name,
                    "level_value": r.final_decision.value,
                    "reason": r.review_reason,
                    "whitelisted": r.is_whitelisted,
                    "rule_hits": r.rule_hits,
                    "hit_count": len(r.rule_hits),
                    "config_version": b_meta.get("config_version", "") or ""
                })
                break

    if not records:
        console.print(f"[yellow]⚠ 未找到商户的历史筛查记录:[/yellow] {merchant_id}")
        console.print("[dim]提示: 支持按商户编号或商户名称查询[/dim]")
        return

    records.sort(key=lambda x: x["created_at"])

    info = records[-1]
    console.rule(f"[bold blue]商户历史筛查: {info['merchant_name']} ({info['merchant_id']})")
    console.print(f"[cyan]共找到[/cyan] {len(records)} 次历史筛查记录，时间跨度: "
          f"{records[0]['created_at'].strftime('%Y-%m-%d')} ~ {records[-1]['created_at'].strftime('%Y-%m-%d')}")

    if len(records) >= 2:
        prev = records[-2]
        curr = records[-1]
        sd = curr["score"] - prev["score"]
        color_map = {"PASS": "green", "REVIEW": "yellow", "REJECT": "red"}
        level_changed = prev["level"] != curr["level"]
        panel_lines = []
        panel_lines.append(
            f"最近一次结论: [{color_map[curr['level'].value]}][bold]{curr['level_display']}[/bold][/{color_map[curr['level'].value]}]"
            f"  (上次: [{color_map[prev['level'].value]}]{prev['level_display']}[/{color_map[prev['level'].value]}])"
        )
        score_color = "red" if sd >= 20 else ("yellow" if sd >= 5 else ("green" if sd <= -20 else "cyan"))
        arrow = "↑" if sd > 0 else ("↓" if sd < 0 else "=")
        panel_lines.append(
            f"分数变化: {prev['score']} → {curr['score']}  [{score_color}]({arrow}{abs(sd)} 分)[/{score_color}]"
        )
        if level_changed:
            panel_lines.append(f"[bold yellow]※ 结论发生变化，需要重点关注[/bold yellow]")
        curr_codes = set(h.rule_code for h in curr["rule_hits"])
        prev_codes = set(h.rule_code for h in prev["rule_hits"])
        new_hits = curr_codes - prev_codes
        cleared_hits = prev_codes - curr_codes
        if new_hits:
            nh_names = [h.rule_name for h in curr["rule_hits"] if h.rule_code in new_hits]
            panel_lines.append(f"[red]新增命中项:[/red] {', '.join(nh_names)}")
        if cleared_hits:
            ch_names = [h.rule_name for h in prev["rule_hits"] if h.rule_code in cleared_hits]
            panel_lines.append(f"[green]已消除项:[/green] {', '.join(ch_names)}")
        console.print(Panel("\n".join(panel_lines), title="[bold]最近两次对比[/bold]", border_style="yellow"))

    ht = Table(title="[bold]历史筛查时序[/bold]", box=box.ROUNDED, header_style="bold blue")
    ht.add_column("#", justify="right", width=3)
    ht.add_column("批次号", style="bold")
    ht.add_column("筛查时间", justify="right")
    ht.add_column("结论")
    ht.add_column("分数", justify="right")
    ht.add_column("命中数", justify="right")
    ht.add_column("原因", overflow="fold")
    ht.add_column("来源文件", overflow="fold")

    for i, r in enumerate(records, 1):
        c = r["level"].color
        marker = ""
        if i == len(records):
            marker = " [cyan]◀最新[/cyan]"
        ht.add_row(
            str(i),
            r["batch_id"],
            r["created_at"].strftime("%m-%d %H:%M"),
            f"[{c}]{r['level_display']}[/{c}]{marker}",
            str(r["score"]),
            str(r["hit_count"]),
            r["reason"] or "-",
            Path(r["input_file"]).name
        )
    console.print(ht)

    if len(records) >= 2:
        scores = [r["score"] for r in records]
        score_table = Table(title="[bold]分数变化趋势[/bold]", box=box.ROUNDED, header_style="bold magenta")
        score_table.add_column("序号", justify="right")
        score_table.add_column("时间", justify="right")
        score_table.add_column("分数", justify="right")
        score_table.add_column("环比", justify="right")
        score_table.add_column("可视化", overflow="fold")
        for i, r in enumerate(records):
            score = r["score"]
            bar_len = min(50, max(0, int(score / 2)))
            bar = "█" * bar_len
            if i == 0:
                delta_str = "-"
                bar = f"[green]{bar}[/green]" if score <= 30 else (f"[yellow]{bar}[/yellow]" if score <= 70 else f"[red]{bar}[/red]")
            else:
                delta = score - records[i-1]["score"]
                if delta > 0:
                    delta_str = f"[red]+{delta}[/red]"
                    bar = f"[red]{bar}[/red]"
                elif delta < 0:
                    delta_str = f"[green]{delta}[/green]"
                    bar = f"[green]{bar}[/green]"
                else:
                    delta_str = "0"
                    bar = f"[cyan]{bar}[/cyan]"
            score_table.add_row(
                str(i + 1),
                r["created_at"].strftime("%m-%d"),
                str(score),
                delta_str,
                bar
            )
        max_s, min_s = max(scores), min(scores)
        avg_s = sum(scores) / len(scores)
        trend_s = scores[-1] - scores[0]
        score_table.add_row(
            "[bold]统计[/bold]", "",
            f"[bold]min={min_s} avg={avg_s:.0f} max={max_s}[/bold]",
            f"[bold]{'+' if trend_s>=0 else ''}{trend_s}[/bold]",
            ""
        )
        console.print(score_table)

    if show_hits:
        console.print()
        for i, r in enumerate(records):
            tag = " ◀最新" if i == len(records) - 1 else ""
            hits_panel_lines = []
            if r["rule_hits"]:
                for h in r["rule_hits"]:
                    sev_color = "red" if h.severity >= 40 else ("yellow" if h.severity >= 20 else "white")
                    hits_panel_lines.append(
                        f"  • [{sev_color}]{h.rule_code}[/{sev_color}] {h.rule_name} "
                        f"({h.severity}分): {h.message}"
                    )
            else:
                hits_panel_lines.append("  [green]无命中，状态良好[/green]")
            console.print(Panel(
                "\n".join(hits_panel_lines),
                title=f"[bold]#{i+1} {r['batch_id']} | {r['created_at'].strftime('%Y-%m-%d')} "
                      f"| {r['level_display']} | {r['score']}分{tag}[/bold]",
                border_style=r["level"].color
            ))

    if export_path:
        fmt = export_format
        if not fmt:
            ext = Path(export_path).suffix.lower()
            if ext == ".json":
                fmt = "json"
            elif ext == ".csv":
                fmt = "csv"
            else:
                console.print(f"[red]✗ 无法推断导出格式，请用 --format 指定 csv 或 json[/red]")
                sys.exit(1)

        out_path = Path(export_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if fmt == "json":
            export_data = []
            for r in records:
                hits_data = [
                    {
                        "rule_code": h.rule_code,
                        "rule_name": h.rule_name,
                        "severity": h.severity,
                        "message": h.message,
                        "suggested_action": h.suggested_action
                    }
                    for h in r["rule_hits"]
                ]
                export_data.append({
                    "batch_id": r["batch_id"],
                    "screen_time": r["created_at"].isoformat(),
                    "merchant_id": r["merchant_id"],
                    "merchant_name": r["merchant_name"],
                    "risk_score": r["score"],
                    "risk_level": r["level_value"],
                    "risk_level_display": r["level_display"],
                    "review_reason": r["reason"] or "",
                    "hit_count": r["hit_count"],
                    "rule_hits": hits_data,
                    "config_version": r["config_version"],
                    "input_file": r["input_file"]
                })
            import json as _json
            with open(out_path, "w", encoding="utf-8") as f:
                _json.dump(export_data, f, ensure_ascii=False, indent=2)
            console.print(f"\n[green]✓ 已导出 JSON:[/green] [link=file://{out_path}]{out_path}[/link]")

        elif fmt == "csv":
            import csv as _csv
            with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = _csv.writer(f)
                writer.writerow([
                    "序号", "批次号", "筛查时间", "商户编号", "商户名称",
                    "风险分数", "风险等级", "等级说明", "命中项数",
                    "命中规则编码", "命中规则名称", "复核原因",
                    "配置版本", "来源文件"
                ])
                for i, r in enumerate(records, 1):
                    hit_codes = ";".join(h.rule_code for h in r["rule_hits"])
                    hit_names = ";".join(h.rule_name for h in r["rule_hits"])
                    writer.writerow([
                        i,
                        r["batch_id"],
                        r["created_at"].strftime("%Y-%m-%d %H:%M:%S"),
                        r["merchant_id"],
                        r["merchant_name"],
                        r["score"],
                        r["level_value"],
                        r["level_display"],
                        r["hit_count"],
                        hit_codes,
                        hit_names,
                        r["reason"] or "",
                        r["config_version"],
                        r["input_file"]
                    ])
            console.print(f"\n[green]✓ 已导出 CSV:[/green] [link=file://{out_path}]{out_path}[/link]")


def main():
    cli()


if __name__ == "__main__":
    main()
