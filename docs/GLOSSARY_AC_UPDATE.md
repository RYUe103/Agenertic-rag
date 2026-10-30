# Aho–Corasick 更新

匹配引擎为 pyahocorasick 2.2.0，Python 导入名为 ahocorasick。不需要修改已上传 JSON 字典，字典会在 Agent 实例加载时重建索引。

更新包的 offline-glossary-wheels 目录包含 CPython 3.12、Linux x86_64、manylinux2014 的二进制 wheel（兼容 Ubuntu 24.04）。在覆盖程序前，使用该服务自己的 .runtime/bin/python 离线安装：

```bash
./.runtime/bin/python -m pip install --no-index --find-links /更新包目录/offline-glossary-wheels pyahocorasick==2.2.0
```

无需编译器或联网。其他 Python 版本/操作系统需要匹配的 wheel，不能直接使用此 CPython 3.12 wheel。

保留大小写折叠、英文标识符边界、重叠匹配、同名多义以及同义词组内 OR、独立概念间 AND 的原有行为。重复的标准名称/别名不会丢失其他词条的义项。匹配结果按文本起点、终点和词条顺序排列。
