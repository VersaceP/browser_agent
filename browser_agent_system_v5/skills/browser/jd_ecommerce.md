---
name: jd_ecommerce
version: 1.0.0
target_websites:
  - jd.com
  - www.jd.com
  - search.jd.com
  - item.jd.com
keywords:
  - 京东
  - JD
  - 京东商城
  - 京东购物
description: 京东电商平台自动化操作技能，支持商品搜索、详情提取和评论抓取
author: system
---

# 京东电商平台自动化技能

## 概述

本技能提供京东电商平台的自动化操作能力，包括：
- 商品搜索（直接 URL 搜索，避免 DOM 操作）
- 商品详情页信息提取
- 异步评论加载和提取
- 评论标签切换（好评/中评/差评）

## 网站特点

- **SPA 架构**：部分页面使用单页应用技术
- **异步加载**：商品详情页的评论区是异步加载的
- **登录要求**：大部分商品详情页和评论需要登录才能查看（重要！）
- **反爬虫机制**：
  - 频繁请求会触发验证码
  - 需要合理设置等待时间
  - 部分内容需要登录才能查看
  - 未登录访问商品详情页会重定向到登录页

## URL 模式

### 直接搜索（推荐）
```
https://search.jd.com/Search?keyword={关键词}&enc=utf-8
```

**优势**：
- 避免复杂的 DOM 操作
- 绕过搜索框输入问题
- 更快速、更可靠

**示例**：
```
https://search.jd.com/Search?keyword=iPhone15&enc=utf-8
https://search.jd.com/Search?keyword=笔记本电脑&enc=utf-8
```

### 商品详情页
```
https://item.jd.com/{商品ID}.html
```

**示例**：
```
https://item.jd.com/100012345678.html
```

## 关键选择器

### 搜索结果页

#### 商品列表容器
```css
#J_goodsList
.gl-warp .gl-item
ul.gl-warp > li
```

#### 单个商品项
```css
.gl-item
li[data-sku]
```

#### 商品标题
```css
.p-name a em
.gl-i-wrap .p-name
```

#### 商品价格
```css
.p-price strong i
.gl-i-wrap .p-price
```

#### 商品链接
```css
.p-name a
.gl-i-wrap .p-img a
```

### 商品详情页

#### 商品标题
```css
.sku-name
.itemInfo-wrap .sku-name
h1.sku-name
```

#### 商品价格
```css
.p-price .price
span.price
.summary-price .price
```

#### 评论区容器
```css
#comment
.comment-list
#CommentList
```

#### 评论标签（好评/中评/差评）
```css
.comment-tab-item
.tab-item
ul.comment-tabs > li
```

#### 单条评论
```css
.comment-item
.comment-con
```

## 操作序列

### 1. 搜索商品（推荐方式）

**直接使用 URL 搜索**：
```
1. 构造搜索 URL：https://search.jd.com/Search?keyword={关键词}&enc=utf-8
2. 使用 navigate 工具打开 URL（wait=3）
3. 等待页面加载完成
4. 提取商品列表
```

**优势**：
- 无需定位搜索框
- 无需处理输入框焦点问题
- 避免搜索按钮点击失败
- 更稳定可靠

### 2. 提取商品详情

**⚡ 直达导航（极其重要！优先级最高！）**：
如果任务描述中已提供商品详情页 URL（如 `https://item.jd.com/100012345678.html`），
**必须直接 navigate 到该 URL**，禁止先打开京东首页再搜索进入！直接导航可以节省大量轮次和 Token。

**重要：京东商品详情页需要登录！**

```
步骤0：检查登录状态
- 如果URL包含"login.aspx"或"passport.jd.com"，说明需要登录
- 调用 wait_user 工具，提示用户登录
- 等待用户完成登录后继续

步骤1：导航到商品详情页
2. 等待页面加载（wait=3）
3. 提取基本信息（标题、价格）
4. 如需评论，执行评论提取流程
```

### 3. 提取商品评论（重要！）

**前置条件：必须先登录京东账号！**

**京东评论区是异步加载的，必须按以下步骤操作**：

```
步骤0：确认已登录
- 检查URL是否包含"login"或"passport"
- 如果需要登录，调用 wait_user 提示用户登录

1. 滚动到评论区域（触发异步加载）
   - 使用 scroll_page 或 run_js 滚动
   
2. 等待 2-3 秒（让评论加载完成）
   
3. 检查评论是否加载
   - 使用 run_js 检查评论元素是否存在
   
4. 提取评论内容
   - 使用 extract_text 或 run_js 提取
   
5. 如需切换评论类型（好评/中评/差评）
   - 定位对应的标签按钮
   - 点击标签
   - 重复步骤 2-4
```

## JavaScript 代码片段

### 检测登录状态
```javascript
// 检查是否在登录页面
const isLoginPage = window.location.href.includes('login') || 
                    window.location.href.includes('passport');

// 检查是否已登录（通过检查用户信息元素）
const userInfo = document.querySelector('.nickname, .user-name, #ttbar-login');
const isLoggedIn = userInfo && !userInfo.textContent.includes('你好，请登录');

return {
    isLoginPage: isLoginPage,
    isLoggedIn: isLoggedIn,
    currentUrl: window.location.href,
    message: isLoginPage ? '需要登录' : (isLoggedIn ? '已登录' : '未登录')
};
```

### 检查评论是否加载
```javascript
const comments = document.querySelectorAll('.comment-item, .comment-con');
return comments.length > 0 ? `已加载 ${comments.length} 条评论` : '评论未加载';
```

### 滚动到评论区
```javascript
const commentSection = document.querySelector('#comment, .comment-list, #CommentList');
if (commentSection) {
    commentSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    return '已滚动到评论区';
}
return '未找到评论区';
```

### 提取商品列表
```javascript
const products = [];
const items = document.querySelectorAll('.gl-item, li[data-sku]');

items.forEach((item, index) => {
    if (index >= 20) return; // 限制提取前20个
    
    const titleElem = item.querySelector('.p-name a em, .p-name a');
    const priceElem = item.querySelector('.p-price strong i, .p-price i');
    const linkElem = item.querySelector('.p-name a, .p-img a');
    
    if (titleElem && priceElem) {
        products.push({
            title: titleElem.textContent.trim(),
            price: priceElem.textContent.trim(),
            link: linkElem ? linkElem.href : ''
        });
    }
});

return JSON.stringify(products, null, 2);
```

### 提取评论内容
```javascript
const comments = [];
const commentItems = document.querySelectorAll('.comment-item, .comment-con');

commentItems.forEach((item, index) => {
    if (index >= 10) return; // 限制提取前10条
    
    const content = item.querySelector('.comment-content, .comment-txt')?.textContent.trim();
    const author = item.querySelector('.comment-author, .user-name')?.textContent.trim();
    const time = item.querySelector('.comment-time, .comment-date')?.textContent.trim();
    
    if (content) {
        comments.push({
            author: author || '匿名',
            content: content,
            time: time || ''
        });
    }
});

return JSON.stringify(comments, null, 2);
```

### 点击评论标签（切换好评/中评/差评）
```javascript
// 查找包含指定文本的标签
const tabs = document.querySelectorAll('.comment-tab-item, .tab-item, ul.comment-tabs > li');
let targetTab = null;

for (const tab of tabs) {
    const text = tab.textContent.trim();
    if (text.includes('好评') || text.includes('中评') || text.includes('差评')) {
        // 根据需要修改这里的条件
        if (text.includes('差评')) {
            targetTab = tab;
            break;
        }
    }
}

if (targetTab) {
    targetTab.click();
    return '已点击评论标签';
}
return '未找到目标标签';
```

## 反爬虫策略

### 等待时间设置
- **页面导航**：wait=3（秒）
- **滚动后等待**：2-3秒
- **点击后等待**：1-2秒
- **评论加载等待**：2-3秒

### 验证码处理
如果页面标题包含"验证"或出现验证码：
1. 立即调用 `wait_user` 工具
2. 提示：`"京东触发了验证码，请手动完成验证后继续"`
3. 等待用户完成验证
4. 继续后续操作

### 登录要求
某些商品或评论可能需要登录：
1. 检测是否跳转到登录页（URL 包含 login）
2. 调用 `wait_user` 工具
3. 提示：`"需要登录京东账号，请手动登录后继续"`
4. 登录完成后继续

## 完整操作示例

### 示例1：搜索并提取商品列表

```
步骤1：构造搜索 URL
https://search.jd.com/Search?keyword=iPhone15&enc=utf-8

步骤2：导航到搜索页
navigate(url="https://search.jd.com/Search?keyword=iPhone15&enc=utf-8", wait=3)

步骤3：提取商品列表
run_js(script="<上面的提取商品列表代码>")

步骤4：保存结果
write_file(filename="jd_products.json", content="<提取的数据>")
```

### 示例2：提取商品详情和评论（完整流程）

```
步骤1：导航到商品详情页
navigate(url="https://item.jd.com/100012345678.html", wait=3)

步骤2：检查登录状态
run_js(script="<检测登录状态代码>")

步骤3：如果需要登录
wait_user(message="请登录京东账号后继续")

步骤4：提取基本信息
extract_text(selector=".sku-name")  # 标题
extract_text(selector=".p-price .price")  # 价格

步骤5：滚动到评论区
run_js(script="<滚动到评论区代码>")

步骤6：等待评论加载
（等待 2-3 秒）

步骤7：检查评论是否加载
run_js(script="<检查评论加载代码>")

步骤8：提取评论
run_js(script="<提取评论内容代码>")

步骤9：（可选）切换到差评
run_js(script="<点击差评标签代码>")
等待 2 秒
run_js(script="<提取评论内容代码>")
```

## 常见问题

### 问题1：搜索结果为空
**原因**：页面未完全加载或选择器失效
**解决方案**：
1. 增加等待时间到 5 秒
2. 使用 run_js 检查页面元素
3. 尝试不同的选择器组合

### 问题2：评论区为空
**原因**：评论是异步加载的，未触发加载，或者需要登录
**解决方案**：
1. **首先确认已登录京东账号**
2. 确保滚动到评论区域
3. 等待足够长的时间（3-5秒）
4. 使用 run_js 检查评论元素是否存在
5. 如果仍未加载，尝试点击评论标签触发加载

### 问题3：价格提取失败
**原因**：价格元素可能有多种展示方式
**解决方案**：
1. 尝试多个价格选择器
2. 使用 run_js 遍历可能的价格元素
3. 检查是否有促销价、会员价等多个价格

### 问题4：触发验证码
**原因**：请求频率过高
**解决方案**：
1. 立即调用 wait_user
2. 让用户手动完成验证
3. 后续增加等待时间
4. 避免短时间内多次请求

## 注意事项

1. **优先使用直接 URL 搜索**：避免复杂的 DOM 操作
2. **评论必须滚动触发**：京东评论是懒加载的
3. **合理设置等待时间**：避免触发反爬虫机制
4. **处理异常情况**：验证码、登录要求等
5. **数据量控制**：不要一次性提取过多数据
6. **尊重网站规则**：遵守 robots.txt 和服务条款

## 更新日志

### v1.0.0 (2026-04-15)
- 初始版本
- 支持直接 URL 搜索
- 支持商品详情提取
- 支持异步评论加载和提取
- 支持评论标签切换
