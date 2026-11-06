# 自编商品资料

这里的耳机、移动电源和亚马逊规则是为了演示 Listing 闭环新写的，不是在售商品。

手册导入和问答在 `knowledge_base/app/import_process`、`knowledge_base/app/query_process`。Listing 评测和人机确认是个人新增，没有删掉导入和问答。

开发集在 `eval/dev.json`，冻结测集在 `eval/frozen_test.json`，目录在 `eval/tasks.json`。每条都有问题、标准要点、证据位置、题型。对外基线只跑冻结测集。任务完成率是草稿题和流程题的成功/失败，写在 `reports/baseline.md` 最上面；hit@k 在后面，不是完成率。重新计算：

```powershell
python -m app.listing_agent.eval
```

这条命令只走原句模板。要拿同一批草稿题对照真实模型，另跑 `python -m app.listing_agent.llm_compare`。模型没返回用量时，费用留空。

留出案例在 `heldout/`，没有参与这 3 个商品的模板调优。分项对照和失败退回写在 `reports/ablation.md`。故障和并发写在 `reports/durability.md`。
