# 京东评论爬取 - 快速修复指南

## 问题

京东评论无法爬取 ❌

## 原因

**京东要求登录才能查看商品详情和评论**

## 解决方案（3步）

### 步骤1：在Agent Prompt中添加登录检查

在 `agent_definition.py` 或任务提示中添加：

```
重要提示：京东商品详情页需要登录才能访问！

在访问商品详情页后，必须执行以下检查：
1. 使用 run_js 检测是否在登录页面
2. 如果需要登录，立即调用 wait_user 工具
3. 等待用户登录完成后继续
```

### 步骤2：使用登录检测脚本

```javascript
// 在导航到商品页后立即执行
const isLoginPage = window.location.href.includes('login') || 
                    window.location.href.includes('passport');

if (isLoginPage) {
    return { needLogin: true, message: '需要登录京东账号' };
}

return { needLogin: false, message: '已登录或无需登录' };
```

### 步骤3：调用 wait_user 工具

```python
# 如果检测到需要登录
wait_user(message="检测到需要登录京东账号，请手动登录后点击继续")
```

## 完整操作流程

```
1. navigate(url="https://item.jd.com/xxx.html", wait=3)

2. run_js(script="<登录检测脚本>")
   → 如果返回 needLogin=true，执行步骤3
   → 如果返回 needLogin=false，跳到步骤4

3. wait_user(message="请登录京东账号后继续")

4. scroll_page(direction="down", amount=500)  # 滚动到评论区

5. 等待 2-3 秒

6. run_js(script="<提取评论脚本>")

7. write_file(filename="comments.json", content="<评论数据>")
```

## 测试方法

运行测试脚本验证：

```bash
python browser_agent_system_v5/tests/test_jd_comment_extraction.py
```

当浏览器打开时，手动登录京东账号，然后观察是否能成功提取评论。

## 常见问题

### Q1: 每次都需要登录吗？

A: 可以实现登录状态持久化，保存cookies后下次自动登录。

### Q2: 如何保存登录状态？

A: 在 `BrowserManager` 中使用 `storage_state` 参数：

```python
# 保存登录状态
await context.storage_state(path="jd_login.json")

# 下次使用
context = await browser.new_context(storage_state="jd_login.json")
```

### Q3: 触发验证码怎么办？

A: 同样使用 `wait_user` 工具，让用户手动完成验证。

## 相关文件

- 📄 Skill文档: `skills/browser/jd_ecommerce.md`
- 🧪 测试脚本: `tests/test_jd_comment_extraction.py`
- 📋 详细报告: `docs/JD_COMMENT_DEBUG_REPORT.md`

## 更新日志

- 2026-04-17: 诊断问题，更新skill文档，创建测试脚本
