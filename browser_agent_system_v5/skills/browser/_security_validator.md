---
name: _security_validator
version: 1.0.0
description: 用于验证第三方技能的系统级技能，检测恶意内容和提示词注入攻击
author: system
target_websites:
  - system
keywords:
  - security
  - validator
---

# 安全验证器系统技能

## 概述

本技能是系统级技能（以 `_` 开头），不会被自动注入到 Browser Agent 中。
它的作用是为第三方技能提供安全验证规则和检测标准。

## 检测规则

### 1. 提示词覆盖关键词检测

检测技能内容中是否包含试图覆盖或忽略系统提示词的关键词。

**检测关键词（英文）**：
- `ignore previous`
- `ignore all previous`
- `override`
- `system prompt`
- `forget`
- `disregard`

**检测关键词（中文）**：
- `忽略之前`
- `忽略所有`
- `覆盖`
- `系统提示`
- `忘记`
- `不要理会`

**风险等级**：HIGH

**处理方式**：立即阻止技能注入

### 2. 过长内容检测

检测技能内容是否超过合理长度限制。

**限制标准**：
- 文件大小：≤ 10KB
- 内容长度：≤ 2000 tokens（粗略估计：1 token ≈ 4 字符）

**计算方式**：
```python
estimated_tokens = len(skill.content) / 3
if estimated_tokens > 2000:
    # 内容过长
```

**风险等级**：MEDIUM

**处理方式**：阻止技能注入，提示内容过长

### 3. 可疑格式检测

检测技能内容中是否包含过多特殊字符，可能是混淆攻击。

**检测标准**：
- 特殊字符占比 > 10%
- 特殊字符定义：除字母、数字、空格、常见标点外的字符
- 常见标点：`.,!?;:()[]{}"\'-—`

**计算方式**：
```python
special_char_count = sum(
    1 for c in skill.content 
    if not c.isalnum() and not c.isspace() 
    and c not in '.,!?;:()[]{}"\'-—'
)
if special_char_count > len(skill.content) * 0.1:
    # 可疑格式
```

**风险等级**：MEDIUM

**处理方式**：阻止技能注入，提示可疑格式

### 4. 敏感操作关键词检测

检测技能内容中是否包含敏感的代码执行关键词。

**检测关键词**：
- `execute`
- `eval`
- `exec`
- `delete`
- `rm -rf`
- `drop table`
- `truncate`
- `__import__`

**风险等级**：LOW（警告）

**处理方式**：
- 不阻止注入（可能是合法的 JavaScript 代码）
- 输出警告日志
- 提示用户谨慎使用

## 风险等级定义

### HIGH（高风险）
- 明确的恶意行为
- 试图攻击系统
- 必须立即阻止

### MEDIUM（中风险）
- 可疑的内容或格式
- 可能是误操作或恶意
- 建议阻止，可配置

### LOW（低风险）
- 包含敏感关键词但可能合法
- 仅警告，不阻止
- 记录日志供审计

## 输出格式

验证结果应返回 JSON 格式：

```json
{
  "safe": true/false,
  "issues": [
    {
      "type": "prompt_override",
      "severity": "high",
      "keyword": "ignore previous",
      "message": "检测到提示词覆盖关键词"
    },
    {
      "type": "content_too_long",
      "severity": "medium",
      "estimated_tokens": 2500,
      "message": "内容过长，超过 2000 tokens 限制"
    }
  ],
  "risk_level": "high"
}
```

### 字段说明

- `safe`: 布尔值，true 表示安全，false 表示不安全
- `issues`: 问题列表，每个问题包含：
  - `type`: 问题类型
  - `severity`: 严重程度（high/medium/low）
  - `keyword`: 触发的关键词（如果适用）
  - `message`: 问题描述
- `risk_level`: 总体风险等级（high/medium/low）

## 验证流程

```
1. 加载技能文件
   ↓
2. 解析 YAML frontmatter
   ↓
3. 提取技能内容
   ↓
4. 执行检测规则 1-4
   ↓
5. 收集所有问题
   ↓
6. 确定总体风险等级
   ↓
7. 返回验证结果
   ↓
8. 根据结果决定是否注入
```

## 实现示例

### Python 实现（在 Hook 中）

```python
async def skill_security_hook(payload: dict) -> HookResult:
    selected_skills = payload.get("selected_skills", [])
    
    prompt_override_keywords = [
        "ignore previous", "ignore all previous", "忽略之前", "忽略所有",
        "override", "覆盖", "system prompt", "系统提示",
        "forget", "忘记", "disregard", "不要理会"
    ]
    
    sensitive_keywords = [
        "execute", "eval", "exec", "delete", "rm -rf",
        "drop table", "truncate", "__import__"
    ]
    
    for skill in selected_skills:
        content_lower = skill.content.lower()
        
        # 检测 1：提示词覆盖
        for keyword in prompt_override_keywords:
            if keyword in content_lower:
                return HookResult(
                    action=HookAction.BLOCK,
                    reason=f"技能 '{skill.name}' 包含可疑的提示词覆盖关键词: '{keyword}'"
                )
        
        # 检测 2：过长内容
        estimated_tokens = len(skill.content) / 4
        if estimated_tokens > 2000:
            return HookResult(
                action=HookAction.BLOCK,
                reason=f"技能 '{skill.name}' 内容过长 (估计 {estimated_tokens:.0f} tokens，限制 2000)"
            )
        
        # 检测 3：可疑格式
        special_char_count = sum(
            1 for c in skill.content 
            if not c.isalnum() and not c.isspace() 
            and c not in '.,!?;:()[]{}"\'-—'
        )
        if special_char_count > len(skill.content) * 0.1:
            return HookResult(
                action=HookAction.BLOCK,
                reason=f"技能 '{skill.name}' 包含过多特殊字符 ({special_char_count} 个)，可能是混淆攻击"
            )
        
        # 检测 4：敏感关键词（仅警告）
        for keyword in sensitive_keywords:
            if keyword in content_lower:
                print(f"[Hook:SKILL_SECURITY] ⚠️ 技能 '{skill.name}' 包含敏感关键词 '{keyword}'，请谨慎使用")
    
    return HookResult(action=HookAction.ALLOW)
```

## 扩展建议

### 未来可添加的检测规则

1. **URL 白名单检测**：
   - 检查技能中的 URL 是否在白名单内
   - 防止访问恶意网站

2. **代码注入检测**：
   - 检测 JavaScript 代码中的危险模式
   - 如 `eval()`, `Function()` 等

3. **数据泄露检测**：
   - 检测是否试图发送数据到外部服务器
   - 如 `fetch()`, `XMLHttpRequest` 等

4. **权限提升检测**：
   - 检测是否试图访问不应访问的资源
   - 如文件系统、系统命令等

5. **签名验证**：
   - 为官方技能添加数字签名
   - 验证技能来源的可信度

## 注意事项

1. **误报处理**：某些合法技能可能包含敏感关键词（如 JavaScript 代码），需要合理判断
2. **规则更新**：随着攻击手段的演进，需要定期更新检测规则
3. **性能考虑**：检测应该快速完成，不影响系统性能
4. **日志记录**：所有检测结果都应记录日志，便于审计和分析
5. **用户反馈**：提供清晰的错误信息，帮助用户理解为什么技能被阻止

## 更新日志

### v1.0.0 (2026-04-15)
- 初始版本
- 实现 4 种基本检测规则
- 定义风险等级和输出格式
