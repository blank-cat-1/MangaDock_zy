# 修改说明文档

## 项目信息
- **原项目**: [dddinmx/MangaDock](https://github.com/dddinmx/MangaDock)
- **修改日期**: 2026-03-31
- **修改者**: blank-cat-1

---

## 修改内容

### 1. 支持多域名访问（2026-03-31）

**问题**: 包子漫画有多个镜像域名，但原项目只支持固定的 `cn.baozimhcn.com`，导致某些域名无法访问时爬虫失效。

**解决方案**: 修改 `app.py` 第 1430-1445 行的 URL 验证逻辑，支持以下域名：
- `cn.baozimhcn.com`（原域名）
- `www.baozimhcn.com`
- `www.baoziman.com`
- `www.bzmh.cn`
- `tw.webmota.com`
- `mxs12.cc` / `www.mxs12.cc`（漫小肆）

**修改代码**:
```python
# 验证URL（支持多个包子漫画域名和 mxs12.cc）
valid_url = False
if re.match(r'^https?://(cn\.|www\.)?baozimh(cn)?\.com/comic/[^/]+/?$', comic_url):
    valid_url = True
elif re.match(r'^https?://www\.baoziman\.com/comic/[^/]+/?$', comic_url):
    valid_url = True
elif re.match(r'^https?://www\.bzmh\.cn/comic/[^/]+/?$', comic_url):
    valid_url = True
elif re.match(r'^https?://tw\.webmota\.com/comic/[^/]+/?$', comic_url):
    valid_url = True
elif re.match(r'^https?://(www\.)?mxs12\.cc/(book/)?[^/]+/?$', comic_url):
    valid_url = True
    # 对于 mxs12.cc，强制使用 CBZ 格式
    comic_format = 2

if not valid_url:
    return render_template('download.html', error='请输入有效的漫画详情页URL（支持 baozimh.com、baoziman.com、bzmh.cn、webmota.com 或 mxs12.cc）')
```

---

### 2. 章节文件使用章节名命名（2026-03-31）

**问题**: 原项目生成的 PDF/CBZ 文件只使用序号命名（如 `01.pdf`），不便于识别章节内容。

**解决方案**: 
1. 在 `crawl_chapter` 函数中提取章节名
2. 新增 `images_to_pdf_with_name` 和 `images_to_cbz_with_name` 函数
3. 文件名格式改为：`序号_章节名.pdf/cbz`

**实现细节**:
- 从网页 title、h1 标签或章节标题元素提取章节名
- 自动清理非法字符（`<>"/\|?*`）
- 限制文件名长度不超过 100 字符
- 如果无法获取章节名，仍使用序号命名

**示例**:
- 修改前: `01.pdf`
- 修改后: `第1话 初次见面.pdf`（纯章节名，不带序号前缀）

**新增/修改的函数**:
- `crawl_chapter()`: 添加章节名提取逻辑
- `images_to_pdf_with_name()`: 支持自定义文件名的 PDF 生成
- `images_to_cbz_with_name()`: 支持自定义文件名的 CBZ 生成
- `_convert_images_to_pdf()`: PDF 转换的公共逻辑提取

**文件名格式**:
- 使用纯章节名命名，不带序号前缀
- 如果无法获取章节名，使用序号（如 `01.pdf`）

---

## 文件说明

| 文件 | 说明 |
|------|------|
| `app.py` | 修改后的主程序文件（包含上述所有修改）|
| `app.py.backup` | 原始文件备份（用于恢复）|
| `MODIFICATIONS.md` | 本修改说明文档 |

---

## 使用方法

### 部署到 Docker

```bash
# 1. 复制修改后的代码到容器
docker cp /path/to/app.py bzmh-downloader:/app/app.py

# 2. 重启容器
docker restart bzmh-downloader
```

### 恢复原始版本

```bash
# 使用备份文件恢复
docker cp /path/to/app.py.backup bzmh-downloader:/app/app.py
docker restart bzmh-downloader
```

---

## 注意事项

1. **备份重要**: 修改前务必备份原始文件，以便有问题时恢复
2. **域名更新**: 如果包子漫画更换了新域名，需要继续更新正则表达式
3. **章节名编码**: 章节名中的特殊字符会被自动清理，确保文件名合法

---

## 后续改进建议

1. 将域名配置提取到配置文件，方便动态更新
2. 添加章节名自定义功能，允许用户手动修改
3. 支持批量重命名已下载的文件
