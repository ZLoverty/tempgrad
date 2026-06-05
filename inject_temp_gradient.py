#!/usr/bin/env python3
"""
inject_temp_gradient.py
-----------------------
在 Bambu Studio / OrcaSlicer 生成的 gcode 中，按层线性插入变温（M104/M109）
和/或变速（M220）指令。温度梯度与速度梯度可单独或同时使用。

用法示例：
  # 仅变温
  python inject_temp_gradient.py input.gcode output.gcode --start-temp 220 --end-temp 200

  # 仅变速（100% → 80%）
  python inject_temp_gradient.py input.gcode output.gcode --start-speed 100 --end-speed 80

  # 同时变温 + 变速
  python inject_temp_gradient.py input.gcode output.gcode \\
      --start-temp 220 --end-temp 200 --start-speed 100 --end-speed 80

  # 指定层范围
  python inject_temp_gradient.py input.gcode output.gcode \\
      --start-temp 220 --end-temp 200 --start-layer 5 --end-layer 80

参数说明：
  input.gcode       输入文件
  output.gcode      输出文件（省略则覆盖输入）
  --start-temp      起始温度（°C），对应 --start-layer 层
  --end-temp        结束温度（°C），对应 --end-layer 层
  --start-speed     起始速度百分比（%），如 100 表示 100%
  --end-speed       结束速度百分比（%）
  --start-layer     开始梯度的层号（从 1 开始，默认第 1 层）
  --end-layer       结束梯度的层号（默认最后一层）
  --wait            温度使用 M109（等待到温），默认 M104（不等待，推荐）
  --extruder        挤出头编号，默认 0（即 T0）
  --dry-run         只打印统计信息，不写文件
"""

import re
import sys
import argparse
from pathlib import Path


# 新版 BambuStudio (P2S 等) 格式：M73 L<n>（1-based，独占一行）
#   M73 L7
LAYER_M73_PATTERN = re.compile(r'^M73 L(\d+)', re.IGNORECASE)
# header 里的总层数：; total layer number: 126
TOTAL_LAYERS_HEADER = re.compile(r';\s*total layer number:\s*(\d+)', re.IGNORECASE)
# 旧版 BambuStudio 格式（备用）：
#   ; layer num/total_layer_count: 7/150
LAYER_NUM_PATTERN = re.compile(r';\s*layer num/total_layer_count:\s*(\d+)/(\d+)', re.IGNORECASE)
# OrcaSlicer / PrusaSlicer 格式（备用）：
#   ;CHANGE_LAYER
LAYER_CHANGE_MARKER = re.compile(r'^;\s*CHANGE_LAYER', re.IGNORECASE)
# 已有的温度指令（用于去重/覆盖判断）
TEMP_CMD_PATTERN = re.compile(r'^\s*M10[49]\s', re.IGNORECASE)
# 已有的速度指令（用于去重/覆盖判断）
SPEED_CMD_PATTERN = re.compile(r'^\s*M220\s', re.IGNORECASE)


def calc_value(layer: int, start_layer: int, end_layer: int,
               start_val: float, end_val: float) -> int:
    """线性插值计算当前层数值，返回整数。"""
    if layer <= start_layer:
        return round(start_val)
    if layer >= end_layer:
        return round(end_val)
    ratio = (layer - start_layer) / (end_layer - start_layer)
    return round(start_val + ratio * (end_val - start_val))


def build_temp_cmd(temp: int, wait: bool, extruder: int) -> str:
    cmd = "M109" if wait else "M104"
    return f"{cmd} S{temp} ; injected temp gradient\n"


def build_speed_cmd(speed: int) -> str:
    return f"M220 S{speed} ; injected speed gradient\n"


def get_total_layers_from_header(lines: list[str]) -> int | None:
    """从 gcode header 中提取总层数（; total layer number: N）。"""
    for line in lines[:200]:
        m = TOTAL_LAYERS_HEADER.search(line)
        if m:
            return int(m.group(1))
    return None


def detect_format(lines: list[str]) -> str:
    """
    检测 gcode 层标记格式。
    返回 'm73'（新版 BambuStudio）、'bambu_old'（旧版）或 'change_layer'
    """
    for line in lines:
        if LAYER_M73_PATTERN.match(line):
            return 'm73'
        if LAYER_NUM_PATTERN.search(line):
            return 'bambu_old'
        if LAYER_CHANGE_MARKER.match(line):
            return 'change_layer'
    return 'm73'  # 默认


def process_gcode(lines: list[str], start_layer: int, end_layer: int,
                  start_temp: float | None, end_temp: float | None,
                  start_speed: float | None, end_speed: float | None,
                  wait: bool, extruder: int, fmt: str) -> tuple[list[str], dict]:
    """
    遍历 gcode 行，在每个目标层开头注入温度/速度指令。
    返回 (新行列表, 统计信息)
    """
    output = []
    stats = {
        'total_layers': 0,
        'injected_temp': 0,
        'injected_speed': 0,
        'layer_temps': {},   # layer -> temp
        'layer_speeds': {},  # layer -> speed%
    }

    current_layer = 0

    i = 0
    while i < len(lines):
        line = lines[i]

        if fmt == 'm73':
            m = LAYER_M73_PATTERN.match(line)
            if m:
                current_layer = int(m.group(1))
                stats['total_layers'] = max(stats['total_layers'], current_layer)
                output.append(line)
                i += 1

                # 跳过紧随其后的 M991（层变更通知）及已有温度/速度指令
                while i < len(lines) and (
                    lines[i].startswith('M991')
                    or TEMP_CMD_PATTERN.match(lines[i])
                    or SPEED_CMD_PATTERN.match(lines[i])
                ):
                    output.append(lines[i])
                    i += 1

                eff_end = end_layer
                if start_layer <= current_layer <= eff_end:
                    if start_temp is not None and end_temp is not None:
                        temp = calc_value(current_layer, start_layer, eff_end,
                                          start_temp, end_temp)
                        output.append(build_temp_cmd(temp, wait, extruder))
                        stats['injected_temp'] += 1
                        stats['layer_temps'][current_layer] = temp
                    if start_speed is not None and end_speed is not None:
                        speed = calc_value(current_layer, start_layer, eff_end,
                                           start_speed, end_speed)
                        output.append(build_speed_cmd(speed))
                        stats['injected_speed'] += 1
                        stats['layer_speeds'][current_layer] = speed
                continue

        elif fmt == 'bambu_old':
            m = LAYER_NUM_PATTERN.search(line)
            if m:
                current_layer = int(m.group(1))
                total = int(m.group(2))
                stats['total_layers'] = total
                output.append(line)
                i += 1

                while i < len(lines) and (
                    TEMP_CMD_PATTERN.match(lines[i]) or SPEED_CMD_PATTERN.match(lines[i])
                ):
                    i += 1

                eff_end = end_layer if end_layer is not None else total
                if start_layer <= current_layer <= eff_end:
                    if start_temp is not None and end_temp is not None:
                        temp = calc_value(current_layer, start_layer, eff_end,
                                          start_temp, end_temp)
                        output.append(build_temp_cmd(temp, wait, extruder))
                        stats['injected_temp'] += 1
                        stats['layer_temps'][current_layer] = temp
                    if start_speed is not None and end_speed is not None:
                        speed = calc_value(current_layer, start_layer, eff_end,
                                           start_speed, end_speed)
                        output.append(build_speed_cmd(speed))
                        stats['injected_speed'] += 1
                        stats['layer_speeds'][current_layer] = speed
                continue

        elif fmt == 'change_layer':
            if LAYER_CHANGE_MARKER.match(line):
                current_layer += 1
                stats['total_layers'] = max(stats['total_layers'], current_layer)
                output.append(line)
                i += 1

                while i < len(lines) and (
                    TEMP_CMD_PATTERN.match(lines[i]) or SPEED_CMD_PATTERN.match(lines[i])
                ):
                    i += 1

                eff_end = end_layer or 999999
                if start_layer <= current_layer <= eff_end:
                    eff_end_calc = end_layer or current_layer
                    if start_temp is not None and end_temp is not None:
                        temp = calc_value(current_layer, start_layer, eff_end_calc,
                                          start_temp, end_temp)
                        output.append(build_temp_cmd(temp, wait, extruder))
                        stats['injected_temp'] += 1
                        stats['layer_temps'][current_layer] = temp
                    if start_speed is not None and end_speed is not None:
                        speed = calc_value(current_layer, start_layer, eff_end_calc,
                                           start_speed, end_speed)
                        output.append(build_speed_cmd(speed))
                        stats['injected_speed'] += 1
                        stats['layer_speeds'][current_layer] = speed
                continue

        output.append(line)
        i += 1

    return output, stats


def _print_gradient_preview(label: str, unit: str,
                             layer_vals: dict, start_layer: int, end_layer: int,
                             start_val: float, end_val: float):
    print(f"\n  {label}范围: 第 {start_layer} 层 → 第 {end_layer} 层")
    print(f"  {label}梯度: {start_val}{unit} → {end_val}{unit}")
    if layer_vals:
        sample_layers = sorted(layer_vals.keys())
        step = max(1, len(sample_layers) // 5)
        print(f"  {label}预览（采样）:")
        shown = set()
        for idx in sample_layers[::step]:
            print(f"    第 {idx:4d} 层 → {layer_vals[idx]}{unit}")
            shown.add(idx)
        last = sample_layers[-1]
        if last not in shown:
            print(f"    第 {last:4d} 层 → {layer_vals[last]}{unit}")


def print_summary(stats: dict, start_layer: int, end_layer,
                  start_temp: float | None, end_temp: float | None,
                  start_speed: float | None, end_speed: float | None):
    total = stats['total_layers']
    eff_end = end_layer if end_layer else total
    print(f"\n{'='*50}")
    print(f"  总层数: {total}")
    if start_temp is not None and end_temp is not None:
        _print_gradient_preview("温度", "°C", stats['layer_temps'],
                                 start_layer, eff_end, start_temp, end_temp)
        print(f"  注入温度指令数: {stats['injected_temp']}")
    if start_speed is not None and end_speed is not None:
        _print_gradient_preview("速度", "%", stats['layer_speeds'],
                                 start_layer, eff_end, start_speed, end_speed)
        print(f"  注入速度指令数: {stats['injected_speed']}")
    print(f"{'='*50}\n")


def main():
    parser = argparse.ArgumentParser(
        description='为 Bambu/Orca gcode 注入线性变温（M104/M109）和/或变速（M220）指令',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('input', help='输入 gcode 文件')
    parser.add_argument('output', nargs='?', help='输出 gcode 文件（省略则覆盖输入）')
    parser.add_argument('--start-temp', type=float, default=None, help='起始温度 °C')
    parser.add_argument('--end-temp',   type=float, default=None, help='结束温度 °C')
    parser.add_argument('--start-speed', type=float, default=None, help='起始速度百分比，如 100')
    parser.add_argument('--end-speed',   type=float, default=None, help='结束速度百分比，如 80')
    parser.add_argument('--start-layer', type=int, default=1, help='开始梯度层（从 1 起，默认 1）')
    parser.add_argument('--end-layer',   type=int, default=None, help='结束梯度层（默认最后一层）')
    parser.add_argument('--wait', action='store_true',
                        help='温度使用 M109（等待到温），默认 M104（不等待）')
    parser.add_argument('--extruder', type=int, default=0, help='挤出头编号，默认 0')
    parser.add_argument('--dry-run', action='store_true', help='只统计不写文件')
    args = parser.parse_args()

    # 参数校验：温度/速度各自成对，且至少指定一组
    temp_partial = (args.start_temp is None) != (args.end_temp is None)
    speed_partial = (args.start_speed is None) != (args.end_speed is None)
    if temp_partial:
        parser.error('--start-temp 和 --end-temp 必须同时指定')
    if speed_partial:
        parser.error('--start-speed 和 --end-speed 必须同时指定')
    if args.start_temp is None and args.start_speed is None:
        parser.error('至少需要指定温度梯度（--start-temp/--end-temp）或速度梯度（--start-speed/--end-speed）之一')

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误：找不到文件 {args.input}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output) if args.output else input_path

    print(f"读取: {input_path}")
    lines = input_path.read_text(encoding='utf-8', errors='replace').splitlines(keepends=True)
    print(f"共 {len(lines)} 行")

    fmt = detect_format(lines)
    print(f"检测到格式: {fmt}")

    # 确定有效总层数
    if args.end_layer is not None:
        end_layer = args.end_layer
    elif fmt == 'm73':
        # 新版 Bambu：直接从 header 读，无需二次扫描
        header_total = get_total_layers_from_header(lines)
        if header_total:
            end_layer = header_total
            print(f"从 header 读取总层数: {end_layer}")
        else:
            print("未找到 header 层数，扫描全文...")
            _, pre_stats = process_gcode(
                lines, 1, 999999,
                args.start_temp, args.end_temp,
                args.start_speed, args.end_speed,
                args.wait, args.extruder, fmt
            )
            end_layer = pre_stats['total_layers']
            print(f"检测到总层数: {end_layer}")
    else:
        print("扫描总层数...")
        _, pre_stats = process_gcode(
            lines, 1, 999999,
            args.start_temp, args.end_temp,
            args.start_speed, args.end_speed,
            args.wait, args.extruder, fmt
        )
        end_layer = pre_stats['total_layers']
        print(f"检测到总层数: {end_layer}")

    # 正式处理
    new_lines, stats = process_gcode(
        lines, args.start_layer, end_layer,
        args.start_temp, args.end_temp,
        args.start_speed, args.end_speed,
        args.wait, args.extruder, fmt
    )

    print_summary(stats, args.start_layer, end_layer,
                  args.start_temp, args.end_temp,
                  args.start_speed, args.end_speed)

    if args.dry_run:
        print("--dry-run 模式，不写入文件。")
        return

    output_path.write_text(''.join(new_lines), encoding='utf-8')
    print(f"已写入: {output_path}")


if __name__ == '__main__':
    main()
