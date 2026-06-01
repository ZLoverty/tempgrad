#!/usr/bin/env python3
"""
inject_temp_gradient.py
-----------------------
在 Bambu Studio / OrcaSlicer 生成的 gcode 中，按层线性插入变温指令。

用法示例：
  python inject_temp_gradient.py input.gcode output.gcode --start-temp 220 --end-temp 200
  python inject_temp_gradient.py input.gcode output.gcode --start-temp 220 --end-temp 200 --start-layer 5 --end-layer 80
  python inject_temp_gradient.py input.gcode output.gcode --start-temp 220 --end-temp 200 --wait

参数说明：
  input.gcode     输入文件
  output.gcode    输出文件
  --start-temp    起始温度（°C），对应 --start-layer 层
  --end-temp      结束温度（°C），对应 --end-layer 层
  --start-layer   开始变温的层号（从 1 开始，默认第 1 层）
  --end-layer     结束变温的层号（默认最后一层）
  --wait          使用 M109（等待到温），默认 M104（不等待，推荐）
  --extruder      挤出头编号，默认 0（即 T0）
  --dry-run       只打印统计信息，不写文件
"""

import re
import sys
import argparse
from pathlib import Path


# 实际 Bambu Studio gcode 层标记格式：
#   ; layer num/total_layer_count: 7/150
LAYER_NUM_PATTERN = re.compile(r';\s*layer num/total_layer_count:\s*(\d+)/(\d+)', re.IGNORECASE)
# OrcaSlicer / PrusaSlicer 兼容格式（备用）：
#   ;LAYER_CHANGE
LAYER_CHANGE_MARKER = re.compile(r';\s*CHANGE_LAYER', re.IGNORECASE)
# 已有的温度指令（用于去重/覆盖判断）
TEMP_CMD_PATTERN = re.compile(r'^\s*M10[49]\s', re.IGNORECASE)


def calc_temp(layer: int, start_layer: int, end_layer: int,
              start_temp: float, end_temp: float) -> int:
    """线性插值计算当前层温度，返回整数°C。"""
    if layer <= start_layer:
        return round(start_temp)
    if layer >= end_layer:
        return round(end_temp)
    ratio = (layer - start_layer) / (end_layer - start_layer)
    return round(start_temp + ratio * (end_temp - start_temp))


def build_temp_cmd(temp: int, wait: bool, extruder: int) -> str:
    cmd = "M109" if wait else "M104"
    return f"{cmd} S{temp} ; injected temp gradient\n"


def detect_format(lines: list[str]) -> str:
    """
    检测 gcode 层标记格式。
    返回 'bambu'（layer num/total_layer_count: N/M）或 'orca_change'（LAYER_CHANGE）
    """
    for line in lines[:500]:
        if LAYER_NUM_PATTERN.search(line):
            return 'bambu'
        if LAYER_CHANGE_MARKER.search(line):
            return 'orca_change'
    return 'bambu'  # 默认


def process_gcode(lines: list[str], start_layer: int, end_layer: int,
                  start_temp: float, end_temp: float,
                  wait: bool, extruder: int, fmt: str) -> tuple[list[str], dict]:
    """
    遍历 gcode 行，在每个目标层开头注入温度指令。
    返回 (新行列表, 统计信息)
    """
    output = []
    stats = {
        'total_layers': 0,
        'injected': 0,
        'layer_temps': {},  # layer -> temp
    }

    current_layer = 0
    layer_just_changed = False
    pending_layer = None  # orca_change 格式下，LAYER_CHANGE 后才知道层号

    i = 0
    while i < len(lines):
        line = lines[i]

        if fmt == 'bambu':
            m = LAYER_NUM_PATTERN.search(line)
            if m:
                current_layer = int(m.group(1))   # 注释里直接是 1-based 层号
                total = int(m.group(2))            # 同时拿到总层数
                stats['total_layers'] = total
                output.append(line)
                i += 1

                # 跳过紧随其后的已有温度指令（避免冲突）
                while i < len(lines) and TEMP_CMD_PATTERN.match(lines[i]):
                    i += 1  # 丢弃原有温度指令

                # 判断是否在变温范围内
                eff_end = end_layer if end_layer is not None else total
                if start_layer <= current_layer <= eff_end:
                    temp = calc_temp(current_layer, start_layer, eff_end, start_temp, end_temp)
                    output.append(build_temp_cmd(temp, wait, extruder))
                    stats['injected'] += 1
                    stats['layer_temps'][current_layer] = temp
                continue

        elif fmt == 'orca_change':
            if LAYER_CHANGE_MARKER.search(line):
                current_layer += 1
                stats['total_layers'] = max(stats['total_layers'], current_layer)
                output.append(line)
                i += 1

                # 跳过已有温度指令
                while i < len(lines) and TEMP_CMD_PATTERN.match(lines[i]):
                    i += 1

                if start_layer <= current_layer <= (end_layer or 999999):
                    temp = calc_temp(current_layer, start_layer,
                                     end_layer or current_layer, start_temp, end_temp)
                    output.append(build_temp_cmd(temp, wait, extruder))
                    stats['injected'] += 1
                    stats['layer_temps'][current_layer] = temp
                continue

        output.append(line)
        i += 1

    return output, stats


def print_summary(stats: dict, start_layer: int, end_layer, start_temp: float, end_temp: float):
    total = stats['total_layers']
    eff_end = end_layer if end_layer else total
    print(f"\n{'='*50}")
    print(f"  总层数: {total}")
    print(f"  变温范围: 第 {start_layer} 层 → 第 {eff_end} 层")
    print(f"  温度范围: {start_temp}°C → {end_temp}°C")
    print(f"  注入指令数: {stats['injected']}")
    if stats['layer_temps']:
        sample_layers = sorted(stats['layer_temps'].keys())
        # 打印几个采样点
        step = max(1, len(sample_layers) // 5)
        print(f"\n  温度预览（采样）:")
        for idx in sample_layers[::step]:
            print(f"    第 {idx:4d} 层 → {stats['layer_temps'][idx]}°C")
        # 确保最后一层也显示
        last = sample_layers[-1]
        if last not in sample_layers[::step]:
            print(f"    第 {last:4d} 层 → {stats['layer_temps'][last]}°C")
    print(f"{'='*50}\n")


def main():
    parser = argparse.ArgumentParser(
        description='为 Bambu/Orca gcode 注入线性变温指令',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('input', help='输入 gcode 文件')
    parser.add_argument('output', nargs='?', help='输出 gcode 文件（省略则覆盖输入）')
    parser.add_argument('--start-temp', type=float, required=True, help='起始温度 °C')
    parser.add_argument('--end-temp', type=float, required=True, help='结束温度 °C')
    parser.add_argument('--start-layer', type=int, default=1, help='开始变温层（从 1 起，默认 1）')
    parser.add_argument('--end-layer', type=int, default=None, help='结束变温层（默认最后一层）')
    parser.add_argument('--wait', action='store_true',
                        help='使用 M109（等待到温），默认 M104（不等待）')
    parser.add_argument('--extruder', type=int, default=0, help='挤出头编号，默认 0')
    parser.add_argument('--dry-run', action='store_true', help='只统计不写文件')
    args = parser.parse_args()

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

    # 第一遍：如果 end_layer 未指定，先扫描总层数
    if args.end_layer is None:
        print("扫描总层数...")
        _, pre_stats = process_gcode(
            lines, 1, 999999,
            args.start_temp, args.end_temp,
            args.wait, args.extruder, fmt
        )
        end_layer = pre_stats['total_layers']
        print(f"检测到总层数: {end_layer}")
    else:
        end_layer = args.end_layer

    # 正式处理
    new_lines, stats = process_gcode(
        lines, args.start_layer, end_layer,
        args.start_temp, args.end_temp,
        args.wait, args.extruder, fmt
    )

    print_summary(stats, args.start_layer, end_layer, args.start_temp, args.end_temp)

    if args.dry_run:
        print("--dry-run 模式，不写入文件。")
        return

    output_path.write_text(''.join(new_lines), encoding='utf-8')
    print(f"已写入: {output_path}")


if __name__ == '__main__':
    main()