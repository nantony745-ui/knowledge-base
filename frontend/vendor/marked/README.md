# Marked

固定版本：18.0.14。项目仅使用浏览器 UMD 文件，许可证为 MIT，原文见 `LICENSE`。

来源：[npm 官方包](https://registry.npmjs.org/marked/-/marked-18.0.14.tgz)，提取 `package/lib/marked.umd.js` 和 `package/LICENSE`，未修改库文件。

下载时验证压缩包 SHA-512，与 npm 版本元数据的 integrity 一致：

```text
sha512-mBHK6FBHuBAlhgRe88w9F0O1AbwwXJUcQibUbC/QcdTbVGAD7aWza+xt3N6oT/jCZx3/OMeS+8rnuiHZcQ9s7A==
```

渲染配置在 `frontend/app.js`：使用 GFM 与换行，覆写 HTML、原始文本、链接和图片渲染，限制输出的可执行内容。扩展接口依据 [Marked 官方文档](https://github.com/markedjs/marked/blob/v18.0.14/docs/USING_PRO.md)。
