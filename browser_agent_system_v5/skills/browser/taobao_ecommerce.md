---
name: taobao_ecommerce
version: 1.0.0
target_websites:
  - taobao.com
  - www.taobao.com
  - s.taobao.com
  - item.taobao.com
  - detail.tmall.com
keywords:
  - 淘宝
  - Taobao
  - 淘宝网
  - 天猫
  - Tmall
description: 淘宝/天猫电商平台自动化操作技能，支持商品搜索、详情提取和评论抓取
author: system
---

# 淘宝/天猫电商平台自动化技能

## 概述

本技能提供淘宝和天猫电商平台的自动化操作能力，包括：
- 商品搜索（直接 URL 搜索，避免 DOM 操作）
- 商品详情页信息提取
- 评论和买家秀提取
- 店铺信息提取

## 网站特点

- **强反爬虫机制**：
  - 频繁请求会触发滑块验证
  - 需要合理设置等待时间
  - 部分内容需要登录才能查看
- **动态内容加载**：大量使用 JavaScript 渲染
- **复杂的页面结构**：选择器可能频繁变化
- **天猫与淘宝**：两个平台结构相似但有差异

## URL 模式

### 直接搜索（推荐）
```
https://s.taobao.com/search?q={关键词}
```

**优势**：
- 避免复杂的 DOM 操作
- 绕过搜索框输入问题
- 更快速、更可靠

**示例**：
```
https://s.taobao.com/search?q=iPhone15
https://s.taobao.com/search?q=连衣裙
```

### 淘宝商品详情页
```
https://item.taobao.com/item.htm?id={商品ID}
```

**示例**：
```
https://item.taobao.com/item.htm?id=123456789012
```

### 天猫商品详情页
```
https://detail.tmall.com/item.htm?id={商品ID}
```

**示例**：
```
https://detail.tmall.com/item.htm?id=123456789012
```

## 关键选择器

### 搜索结果页

#### 商品列表容器
```css
.items .item
.m-itemlist .item
div[data-category="item"]
```

#### 单个商品项
```css
.item
.Card--doubleCardWrapper--L2XFE73
```

#### 商品标题
```css
.title a
.Card--mainPicAndDesc--wvcDXaK .title
```

#### 商品价格
```css
.price strong
.priceInt
.Price--priceInt--ZlsSi_M
```

#### 商品链接
```css
.pic a
.Card--mainPicWrapper--IV2pXBN a
```

### 商品详情页（淘宝）

#### 商品标题
```css
.tb-detail-hd h1
.ItemHeader--mainTitle--qGXyD8Y
h1[data-spm="1000983"]
```

#### 商品价格
```css
.tb-rmb-num
.Price--priceText--c08_X73
span[class*="priceText"]
```

#### 评论区
```css
#J_Reviews
.Rate--root--1SL8Yvv
```

### 商品详情页（天猫）

#### 商品标题
```css
.tb-detail-hd h1
h1[data-spm]
```

#### 商品价格
```css
.tm-price
.Price--priceText--c08_X73
```

## 操作序列

### 1. 搜索商品（推荐方式）

**直接使用 URL 搜索**：
```
1. 构造搜索 URL：https://s.taobao.com/search?q={关键词}
2. 使用 navigate 工具打开 URL（wait=3）
3. 等待页面加载完成
4. 检查是否触发验证码
5. 提取商品列表
```

### 2. 提取商品详情

```
1. 导航到商品详情页
2. 等待页面加载（wait=3-5）
3. 检查是否需要登录
4. 提取基本信息（标题、价格、店铺）
5. 如需评论，滚动到评论区
6. 提取评论内容
```

### 3. 处理验证码

淘宝的滑块验证码无法自动通过：
```
1. 检测验证码页面（标题包含"验证"或出现滑块）
2. 立即调用 wait_user
3. 提示用户手动完成验证
4. 验证完成后继续
```

## JavaScript 代码片段

### 检查是否触发验证码
```javascript
const title = document.title;
const hasSlider = document.querySelector('.nc-container, .nc_wrapper');
if (title.includes('验证') || hasSlider) {
    return '检测到验证码';
}
return '无验证码';
```

### 提取商品列表
```javascript
const products = [];
const items = document.querySelectorAll('.item, .Card--doubleCardWrapper--L2XFE73');

items.forEach((item, index) => {
    if (index >= 20) return; // 限制提取前20个
    
    const titleElem = item.querySelector('.title a, .Card--mainPicAndDesc--wvcDXaK .title');
    const priceElem = item.querySelector('.price strong, .priceInt, .Price--priceInt--ZlsSi_M');
    const linkElem = item.querySelector('.pic a, .Card--mainPicWrapper--IV2pXBN a');
    
    if (titleElem) {
        products.push({
            title: titleElem.textContent.trim(),
            price: priceElem ? priceElem.textContent.trim() : '价格未知',
            link: linkElem ? linkElem.href : ''
        });
    }
});

return JSON.stringify(products, null, 2);
```

### 提取商品详情（淘宝）
```javascript
const detail = {};

// 标题
const titleElem = document.querySelector('.tb-detail-hd h1, .ItemHeader--mainTitle--qGXyD8Y, h1[data-spm="1000983"]');
if (titleElem) {
    detail.title = titleElem.textContent.trim();
}

// 价格
const priceElem = document.querySelector('.tb-rmb-num, .Price--priceText--c08_X73, span[class*="priceText"]');
if (priceElem) {
    detail.price = priceElem.textContent.trim();
}

// 店铺名称
const shopElem = document.querySelector('.tb-shop-name a, .ShopHeader--name--1WJz8vv');
if (shopElem) {
    detail.shop = shopElem.textContent.trim();
}

// 销量
const salesElem = document.querySelector('.tb-sell-counter, .SalesPoint--subText--j8S3mPj');
if (salesElem) {
    detail.sales = salesElem.textContent.trim();
}

return JSON.stringify(detail, null, 2);
```

### 滚动到评论区
```javascript
const commentSection = document.querySelector('#J_Reviews, .Rate--root--1SL8Yvv, .ReviewsTab--root--1Gy8Yvv');
if (commentSection) {
    commentSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    return '已滚动到评论区';
}
return '未找到评论区';
```

### 提取评论内容
```javascript
const comments = [];
const commentItems = document.querySelectorAll('.rate-grid tr, .Comment--root--1Gy8Yvv');

commentItems.forEach((item, index) => {
    if (index >= 10) return; // 限制提取前10条
    
    const content = item.querySelector('.rate-content, .Comment--content--1Gy8Yvv')?.textContent.trim();
    const author = item.querySelector('.rate-user-info, .Comment--userName--1Gy8Yvv')?.textContent.trim();
    const time = item.querySelector('.rate-date, .Comment--date--1Gy8Yvv')?.textContent.trim();
    
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

## 反爬虫策略

### 等待时间设置
- **页面导航**：wait=3-5（秒）
- **滚动后等待**：2-3秒
- **点击后等待**：2秒
- **验证码处理**：必须人工介入

### 验证码处理（重要！）

淘宝的滑块验证码非常严格：
1. **检测验证码**：
   - 页面标题包含"验证"
   - 出现滑块元素（.nc-container）
   
2. **立即停止自动化**：
   - 调用 `wait_user` 工具
   - 提示：`"淘宝触发了滑块验证，请手动完成验证后继续"`
   
3. **等待用户完成**：
   - 用户手动拖动滑块
   - 验证通过后点击继续
   
4. **继续任务**：
   - 验证完成后继续后续操作

### 登录要求

某些商品或功能需要登录：
1. 检测登录页面（URL 包含 login）
2. 调用 `wait_user` 工具
3. 提示：`"需要登录淘宝账号，请手动登录后继续"`
4. 登录完成后继续

### 频率控制

- **避免高频请求**：每次请求间隔至少 3-5 秒
- **限制数据量**：不要一次性提取过多商品
- **分批处理**：大量数据分多次请求

## 完整操作示例

### 示例1：搜索并提取商品列表

```
步骤1：构造搜索 URL
https://s.taobao.com/search?q=iPhone15

步骤2：导航到搜索页
navigate(url="https://s.taobao.com/search?q=iPhone15", wait=5)

步骤3：检查验证码
run_js(script="<检查验证码代码>")

步骤4：如果有验证码
wait_user(message="淘宝触发了滑块验证，请手动完成验证后继续")

步骤5：提取商品列表
run_js(script="<提取商品列表代码>")

步骤6：保存结果
write_file(filename="taobao_products.json", content="<提取的数据>")
```

### 示例2：提取商品详情

```
步骤1：导航到商品详情页
navigate(url="https://item.taobao.com/item.htm?id=123456789012", wait=5)

步骤2：检查是否需要登录
（检查 URL 是否跳转到登录页）

步骤3：提取基本信息
run_js(script="<提取商品详情代码>")

步骤4：滚动到评论区
run_js(script="<滚动到评论区代码>")

步骤5：等待评论加载
（等待 2-3 秒）

步骤6：提取评论
run_js(script="<提取评论内容代码>")
```

## 常见问题

### 问题1：频繁触发验证码
**原因**：淘宝的反爬虫机制非常严格
**解决方案**：
1. 增加等待时间（5秒以上）
2. 减少请求频率
3. 使用已登录的浏览器 profile
4. 必要时让用户手动完成验证

### 问题2：选择器失效
**原因**：淘宝页面结构经常变化
**解决方案**：
1. 使用多个备选选择器
2. 使用 run_js 动态查找元素
3. 根据元素特征（class 包含关键词）查找

### 问题3：价格提取不准确
**原因**：价格展示方式多样（促销价、会员价等）
**解决方案**：
1. 尝试多个价格选择器
2. 提取所有价格相关元素
3. 在结果中标注价格类型

### 问题4：需要登录才能查看
**原因**：某些商品或评论需要登录
**解决方案**：
1. 检测登录要求
2. 调用 wait_user 让用户登录
3. 使用已登录的浏览器 profile

## 淘宝 vs 天猫差异

### URL 差异
- 淘宝：`item.taobao.com`
- 天猫：`detail.tmall.com`

### 页面结构差异
- 天猫页面通常更规范
- 淘宝页面变化更频繁
- 选择器需要分别适配

### 反爬虫强度
- 天猫相对宽松
- 淘宝更严格

## 注意事项

1. **优先使用直接 URL 搜索**：避免复杂的 DOM 操作
2. **必须处理验证码**：淘宝验证码无法绕过，必须人工介入
3. **合理设置等待时间**：至少 3-5 秒
4. **控制请求频率**：避免被封 IP
5. **使用登录状态**：某些功能需要登录
6. **选择器要有备选**：页面结构经常变化
7. **尊重网站规则**：遵守 robots.txt 和服务条款
8. **数据量控制**：不要一次性提取过多数据

## 更新日志

### v1.0.0 (2026-04-15)
- 初始版本
- 支持直接 URL 搜索
- 支持淘宝和天猫商品详情提取
- 支持评论提取
- 包含验证码处理策略
