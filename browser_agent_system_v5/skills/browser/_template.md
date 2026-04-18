---
# 技能元数据（YAML Frontmatter）
# 所有字段都是必需的，除非标记为可选

# 技能唯一标识符（必需）
# 使用小写字母和下划线，例如：jd_ecommerce, taobao_search
name: example_skill

# 语义化版本号（必需）
# 格式：MAJOR.MINOR.PATCH
# 例如：1.0.0, 2.1.3
version: 1.0.0

# 目标网站列表（必需）
# 当任务中包含这些域名时，此技能会被自动选择
# 支持主域名和子域名
target_websites:
  - example.com
  - www.example.com
  - shop.example.com

# 关键词列表（必需）
# 当任务描述中包含这些关键词时，此技能会被自动选择
# 支持中英文，不区分大小写
keywords:
  - 示例
  - example
  - 样例网站

# 技能描述（必需）
# 简要说明此技能的用途和适用场景
description: 示例网站自动化操作技能模板

# 作者信息（必需）
# 可以是用户名、团队名或 "system"（系统内置技能）
author: system

---

# 技能内容（Markdown 格式）

## 概述

简要描述此技能提供的自动化能力和适用场景。

## 网站特点

描述目标网站的技术特点：
- 是否使用 SPA（单页应用）
- 是否有反爬虫机制
- 页面加载方式（同步/异步）
- 特殊的认证或登录要求

## URL 模式

### 搜索页面
```
https://example.com/search?q={关键词}
```

### 商品详情页
```
https://example.com/product/{商品ID}
```

### 用户中心
```
https://example.com/user/profile
```

## 关键选择器

### 搜索框
```css
input#search-input
input[name="keyword"]
.search-box input
```

### 搜索按钮
```css
button.search-btn
button[type="submit"]
.search-box button
```

### 商品列表
```css
.product-list .product-item
ul.products > li
div[data-product-id]
```

### 商品标题
```css
.product-title
h3.title a
.item-name
```

### 商品价格
```css
.product-price
span.price
.price-current
```

## 操作序列

### 搜索商品
1. 导航到首页或搜索页
2. 定位搜索框（使用上述选择器）
3. 输入搜索关键词
4. 点击搜索按钮或按回车
5. 等待搜索结果加载（2-3秒）
6. 提取商品列表

### 获取商品详情
1. 导航到商品详情页
2. 等待页面完全加载（3-5秒）
3. 滚动到评论区域（如果需要）
4. 提取商品信息（标题、价格、描述等）
5. 提取评论信息（如果需要）

## JavaScript 代码片段

### 检查页面是否加载完成
```javascript
return document.readyState === 'complete' && 
       document.querySelectorAll('.product-item').length > 0;
```

### 提取商品列表
```javascript
const products = [];
document.querySelectorAll('.product-item').forEach(item => {
    products.push({
        title: item.querySelector('.product-title')?.textContent.trim(),
        price: item.querySelector('.product-price')?.textContent.trim(),
        link: item.querySelector('a')?.href
    });
});
return JSON.stringify(products);
```

### 滚动到评论区
```javascript
const commentSection = document.querySelector('.comment-section');
if (commentSection) {
    commentSection.scrollIntoView({ behavior: 'smooth' });
    return 'scrolled';
}
return 'not found';
```

## 反爬虫策略

### 等待时间
- 页面导航后等待：2-3秒
- 点击操作后等待：1-2秒
- 滚动操作后等待：1秒

### 验证码处理
如果遇到验证码页面：
1. 立即调用 `wait_user` 工具
2. 告知用户需要手动完成验证
3. 等待用户完成后继续

### 登录要求
如果需要登录：
1. 检测登录页面特征（如 URL 包含 /login）
2. 调用 `wait_user` 工具
3. 提示用户手动登录
4. 登录完成后继续任务

## 常见问题

### 问题1：搜索框无法输入
**原因**：页面可能使用了自定义输入组件
**解决方案**：
1. 尝试使用 `fill_form` 工具（自动降级）
2. 如果失败，使用 `run_js` 直接设置 value
3. 触发 input 和 change 事件

### 问题2：商品列表未加载
**原因**：异步加载或需要滚动触发
**解决方案**：
1. 等待更长时间（5秒）
2. 滚动页面触发懒加载
3. 使用 `run_js` 检查元素是否存在

### 问题3：评论区为空
**原因**：评论是异步加载的
**解决方案**：
1. 滚动到评论区域
2. 等待2-3秒
3. 使用 `run_js` 检查评论是否加载
4. 如果仍未加载，可能需要点击"查看更多"按钮

## 注意事项

1. **尊重网站规则**：遵守 robots.txt 和服务条款
2. **控制频率**：避免高频请求导致 IP 被封
3. **数据隐私**：不要抓取用户隐私数据
4. **错误处理**：遇到异常时优雅降级，不要无限重试
5. **日志记录**：记录关键操作步骤，便于调试

## 更新日志

### v1.0.0 (2024-01-01)
- 初始版本
- 支持基本搜索和商品详情提取
