# JSON 行业字典

在“Agent 设置 → 行业术语字典”选择多个 JSON 文件，保存 Agent 后生效。每个 Agent 最多 20 份字典，独立绑定自己的知识库；不进入资料库和 Dify。相同文件名再次导入会替换草稿中的该字典。可逐个启用、禁用、删除。

```json
{"version":1,"entries":[{"term":"PWR","aliases":["pressurized water reactor","压水堆"],"data":{"定义":"……","适用范围":"……"}}]}
```

term 是非空字符串，aliases 是非空字符串组成的数组（可以为空数组），data 可以为任意 JSON 信息。索引仅匹配 term 和 aliases，不匹配 data。英文不区分大小写，英文标识符有边界保护。JSON 字典使用内存 Aho–Corasick 自动机匹配，不调用字典识别 LLM；旧 Markdown 字典仍使用原来的模型识别。

多个启用字典使用 pyahocorasick 2.2.0 建立共享的 Aho–Corasick 自动机，扫描整句，不调用匹配 LLM。支持重叠词条，同名多义保留来源及 data 给 Planner 判断，系统不自动混合不同义项的别名。未消歧时保持原词检索；Planner 可以选用某义项的独有名称。

JSON 字典命中后，grep_files 和 keyword_search 自动扩展同义名称，组内 OR，独立概念间按 Planner 的 all/any 组合。PWR 泵默认是 (PWR OR pressurized water reactor OR 压水堆) AND 泵。作用范围仍服从 filename/title/body/all。文件清单不包含字典文件。vector_search 由 Planner 参考字典上下文生成语义查询。

属性作为字典资料交给模型，不作为独立业务事实；问答必须区分字典定义和业务文件证据。页面提供 JSON 模板下载。
