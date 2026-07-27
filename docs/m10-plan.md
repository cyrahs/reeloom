# M10 计划：Movie 领域支持

## 交付范围

M10 在保持 Anime/TV 兼容的前提下，通过既有 Web UI 和 API 打通 Movie 的配置、
discovery、识别、preview、revision、manual/automatic apply、reapply、rollback
与 recovery。

固定命名：

```text
<标题> (<年份>) {tmdb-<id>}/<标题> (<年份>).<video-ext>
<标题> (<年份>) {tmdb-<id>}/<标题> (<年份>).chs.<subtitle-ext>
```

繁体与未知中文分别使用 `cht`、`chi`；同变体同扩展名的多个字幕稳定编号。

## 增量

1. M10.0：ADR、威胁模型、兼容基线和 strict schema。
2. M10.1：Movie identity、单正片 mapping、命名与 unmapped。
3. M10.2：独立 initial/amendment canonical plan 和 Executor 接入。
4. M10.3：Movie phase/events、专用 Agent tools 和 scripted model。
5. M10.4：worker、API enum、Web 配置与 initial apply 闭环。
6. M10.5：revision、completed-layout reapply、identity correction 与 no-op。
7. M10.6：离线、PostgreSQL、三浏览器回归和文档。

## 非目标

不支持 multipart、多正片、extras、trailer、NFO、海报、已有 Movie 目录合并、
新网络 adapter、Agent 路径权限或通用媒体框架。
