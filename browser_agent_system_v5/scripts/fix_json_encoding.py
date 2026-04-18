#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复JSON文件中的Unicode转义字符问题

使用方法：
    python scripts/fix_json_encoding.py worktrees/task_1776176035/lead/reddit_posts_raw.json
"""

import json
import sys
from pathlib import Path


def fix_json_encoding(file_path: str, backup: bool = True) -> None:
    """修复JSON文件的Unicode编码问题
    
    Args:
        file_path: JSON文件路径
        backup: 是否创建备份文件
    """
    path = Path(file_path)
    
    if not path.exists():
        print(f"错误：文件不存在 - {file_path}")
        return
    
    if not path.suffix == '.json':
        print(f"警告：文件不是JSON格式 - {file_path}")
        return
    
    print(f"正在处理: {file_path}")
    
    # 创建备份
    if backup:
        backup_path = path.with_suffix('.json.bak')
        backup_path.write_bytes(path.read_bytes())
        print(f"已创建备份: {backup_path}")
    
    # 读取并重新保存
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"修复成功！")
        
        # 显示修复前后对比
        if 'posts' in data and len(data['posts']) > 0:
            print("\n修复示例（前3条标题）：")
            for i, post in enumerate(data['posts'][:3], 1):
                print(f"{i}. {post.get('title', 'N/A')}")
        
    except json.JSONDecodeError as e:
        print(f"错误：JSON解析失败 - {e}")
    except Exception as e:
        print(f"错误：{e}")


def fix_directory(dir_path: str, pattern: str = "*.json") -> None:
    """批量修复目录下的所有JSON文件
    
    Args:
        dir_path: 目录路径
        pattern: 文件匹配模式
    """
    directory = Path(dir_path)
    
    if not directory.exists():
        print(f"错误：目录不存在 - {dir_path}")
        return
    
    json_files = list(directory.rglob(pattern))
    
    if not json_files:
        print(f"未找到匹配的JSON文件: {pattern}")
        return
    
    print(f"找到 {len(json_files)} 个JSON文件")
    print("=" * 60)
    
    for json_file in json_files:
        fix_json_encoding(str(json_file))
        print("-" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使用方法:")
        print("  修复单个文件:")
        print("    python scripts/fix_json_encoding.py path/to/file.json")
        print()
        print("  修复整个目录:")
        print("    python scripts/fix_json_encoding.py path/to/directory/ --dir")
        sys.exit(1)
    
    target = sys.argv[1]
    is_directory = "--dir" in sys.argv
    
    if is_directory:
        fix_directory(target)
    else:
        fix_json_encoding(target)
